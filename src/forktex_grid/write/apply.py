# Copyright (C) 2026 FORKTEX S.R.L.
#
# SPDX-License-Identifier: AGPL-3.0-or-later OR LicenseRef-ForkTex-Commercial
#
# This file is part of ForkTex Grid.
#
# For commercial licensing -- including use in proprietary products, SaaS
# deployments, or any context where AGPL obligations cannot be met -- you
# MUST obtain a commercial license from FORKTEX S.R.L. (info@forktex.com).
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.


from __future__ import annotations

import uuid

import sqlalchemy as sa
from forktex.database.locks import advisory_key, advisory_lock_held, xact_lock
from forktex.log import get_logger
from forktex.types import BaseWireValueObject
from pydantic import ConfigDict, computed_field
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from forktex_grid.domain.diff import Change, ChangeOp, ChangeSet, diff
from forktex_grid.domain.enums import IndexBuilds
from forktex_grid.domain.schema import ColumnSpec, IndexSpec, Schema, TableSpec
from forktex_grid.errors import BadRequestError
from forktex_grid.persist import catalog, indexes
from forktex_grid.persist.extensions import require_trgm
from forktex_grid.persist.indexes import IndexOutcome
from forktex_grid.persist.locks import engine_turn, polled_advisory_lock, schema_exclusive
from forktex_grid.persist.models import pg_schema_of
from forktex_grid.persist.tx import atomic
from forktex_grid.write.conform import backfill_default, ensure_rows_fit

logger = get_logger(__name__)


class ApplyOptions(BaseWireValueObject):
    model_config = ConfigDict(extra="forbid")

    prune: bool = False
    allow_destructive: bool = False
    dry_run: bool = False
    index_builds: IndexBuilds = IndexBuilds.concurrent


class ApplyReport(BaseWireValueObject):
    plan: ChangeSet
    options: ApplyOptions
    indexes: tuple[IndexOutcome, ...] = ()

    @computed_field
    @property
    def complete(self) -> bool:
        return all(i.ok for i in self.indexes)


def in_one_transaction(options: ApplyOptions, what: str) -> ApplyOptions:
    if "index_builds" in options.model_fields_set and options.index_builds is IndexBuilds.concurrent:
        raise BadRequestError(f"{what} is one transaction: its index builds are inline")
    return options.model_copy(update={"index_builds": IndexBuilds.inline})


def _lock_key(namespace: str) -> int:
    return advisory_key("grid.apply", namespace)


async def lock_namespace(session: AsyncSession, namespace: str) -> None:
    await xact_lock(session, _lock_key(namespace))


def _builds_trgm(change: Change) -> bool:
    return (
        change.op is ChangeOp.create_index and isinstance(change.desired, IndexSpec) and change.desired.kind == "trgm"
    )


async def _plan(
    session: AsyncSession, namespace: str, desired: Schema, *, prune: bool, allow_destructive: bool
) -> tuple[ChangeSet, Schema]:
    actual = await catalog.hydrate(session, namespace)
    effective = actual.merged_with(desired, prune=prune).check()
    changes = diff(actual, effective)
    if any(_builds_trgm(c) for c in changes.changes):
        await require_trgm(session, "a trgm index")
    if changes.destructive and not allow_destructive:
        targets = [c.target for c in changes.destructive]
        raise BadRequestError(
            f"refusing {len(targets)} destructive change(s) without allow_destructive: {targets}",
            details={"changes": targets},
        )
    return changes, effective


def _key_of(change: Change) -> str:
    if change.key is None:
        raise BadRequestError(f"{change.target}: names no column")
    return change.key


def _spec[S: (TableSpec, ColumnSpec)](change: Change, kind: type[S]) -> S:
    if not isinstance(change.desired, kind):
        raise BadRequestError(f"{change.target}: expected a {kind.__name__}")
    return change.desired


async def _write(session: AsyncSession, namespace: str, plan: ChangeSet, effective: Schema) -> dict[str, uuid.UUID]:
    dropped: dict[str, uuid.UUID] = {}
    ids: dict[str, uuid.UUID] = {}
    reindexed: set[str] = set()

    async def table_id(slug: str) -> uuid.UUID:
        if slug not in ids:
            ids.update(await catalog.table_ids(session, namespace))
        return ids[slug]

    for change in plan.changes:
        match change.op:
            case ChangeOp.create_table:
                ids[change.table] = await catalog.create_table(session, namespace, _spec(change, TableSpec))
            case ChangeOp.alter_table:
                await catalog.alter_table(session, namespace, _spec(change, TableSpec))
            case ChangeOp.add_column:
                column = _spec(change, ColumnSpec)
                await catalog.add_column(session, await table_id(change.table), column)
                await backfill_default(session, await table_id(change.table), column)
                if column.is_required:
                    await ensure_rows_fit(session, await table_id(change.table), column)
            case ChangeOp.alter_column:
                column = _spec(change, ColumnSpec)
                if change.destructive:
                    await ensure_rows_fit(session, await table_id(change.table), column)
                await catalog.alter_column(session, await table_id(change.table), column)
            case ChangeOp.drop_column:
                await catalog.archive_column(session, await table_id(change.table), _key_of(change))
            case ChangeOp.drop_table:
                dropped[change.table] = await catalog.archive_table(session, namespace, change.table)
            case ChangeOp.create_index | ChangeOp.drop_index:
                reindexed.add(change.table)
    for slug in sorted(reindexed):
        await catalog.set_indexes(session, namespace, effective.table(slug))
    return dropped


async def _physical_targets(session: AsyncSession, namespace: str) -> dict[str, tuple[uuid.UUID, TableSpec]]:
    schema = await catalog.hydrate(session, namespace)
    ids = await catalog.table_ids(session, namespace)
    return {slug: (ids[slug], spec) for slug, spec in schema.by_slug.items()}


async def apply_schema(session: AsyncSession, namespace: str, desired: Schema, options: ApplyOptions) -> ApplyReport:
    if options.dry_run:
        plan, _ = await _plan(session, namespace, desired, prune=options.prune, allow_destructive=True)
        return ApplyReport(plan=plan, options=options)
    if options.index_builds is IndexBuilds.inline:
        return await _apply_inline(session, namespace, desired, options)
    return await _apply_concurrently(session, namespace, desired, options)


def _log(namespace: str, report: ApplyReport) -> None:
    if report.plan.changes:
        logger.info(
            "grid: applied schema",
            extra={"namespace": namespace, "changes": [c.target for c in report.plan.changes]},
        )
    failed = [i.index for i in report.indexes if not i.ok]
    if failed:
        logger.warning("grid: index builds failed", extra={"namespace": namespace, "indexes": failed})


async def _apply_inline(session: AsyncSession, namespace: str, desired: Schema, options: ApplyOptions) -> ApplyReport:
    await lock_namespace(session, namespace)
    schema = pg_schema_of(session)
    async with atomic(session):
        await schema_exclusive(session, namespace)
        plan, effective = await _plan(
            session, namespace, desired, prune=options.prune, allow_destructive=options.allow_destructive
        )
        dropped = await _write(session, namespace, plan, effective)
        for table_id, spec in (await _physical_targets(session, namespace)).values():
            await indexes.reconcile(session, schema, table_id, spec)
        for table_id in dropped.values():
            await indexes.reconcile(session, schema, table_id, None)
    report = ApplyReport(plan=plan, options=options)
    _log(namespace, report)
    return report


async def _apply_concurrently(
    session: AsyncSession, namespace: str, desired: Schema, options: ApplyOptions
) -> ApplyReport:
    if session.new or session.dirty or session.deleted:
        raise BadRequestError(
            "a concurrent apply commits its session, and this one holds unflushed changes; "
            'commit them first, or apply with index_builds="inline"'
        )
    if await advisory_lock_held(session, _lock_key(namespace), by_this_session=True):
        raise BadRequestError(
            "this transaction already holds this namespace's schema lock: it changed the schema and has not "
            'committed (commit first, or apply with index_builds="inline"), or a pooler in transaction mode '
            "handed it a server connection where an earlier apply's lock leaked (point grid at Postgres or a "
            "session-mode pool)"
        )
    bind = session.get_bind()
    if not isinstance(bind, sa.Engine):
        raise BadRequestError("a concurrent apply needs a session bound to an engine")
    schema = pg_schema_of(session)
    await session.commit()
    async with engine_turn(bind), polled_advisory_lock(AsyncEngine(bind), _lock_key(namespace)) as conn:
        async with atomic(session):
            await schema_exclusive(session, namespace)
            plan, effective = await _plan(
                session, namespace, desired, prune=options.prune, allow_destructive=options.allow_destructive
            )
            dropped = await _write(session, namespace, plan, effective)
        targets = await _physical_targets(session, namespace)
        await session.commit()

        outcomes: list[IndexOutcome] = []
        for slug, (table_id, spec) in sorted(targets.items()):
            outcomes += await indexes.reconcile_concurrently(conn, schema, table_id, slug, spec)
        for slug, table_id in sorted(dropped.items()):
            outcomes += await indexes.reconcile_concurrently(conn, schema, table_id, slug, None)
    report = ApplyReport(plan=plan, options=options, indexes=tuple(outcomes))
    _log(namespace, report)
    return report


__all__ = ["ApplyOptions", "ApplyReport", "apply_schema", "in_one_transaction", "lock_namespace"]
