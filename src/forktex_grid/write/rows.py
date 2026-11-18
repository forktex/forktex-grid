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

import contextlib
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence

import sqlalchemy as sa
from forktex.iso import now
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_grid.domain.resolved import ResolvedTable
from forktex_grid.domain.row import ExternalRef, Row, external_ref_text
from forktex_grid.domain.values import stored_payload
from forktex_grid.errors import AlreadyExistsError, BadRequestError
from forktex_grid.persist import catalog
from forktex_grid.persist.locks import schema_shared
from forktex_grid.persist.models import GridRow
from forktex_grid.persist.tx import atomic
from forktex_grid.read.rows import load_by_external_ref, load_row, to_row
from forktex_grid.write.links import ArchivePlan, ensure_targets_live


@contextlib.asynccontextmanager
async def _naming_unique_columns(table: ResolvedTable) -> AsyncIterator[None]:
    try:
        yield
    except AlreadyExistsError as exc:
        unique = [c.key for c in table.spec.columns if c.is_unique] + [
            "+".join(i.column_keys) for i in table.spec.indexes if i.is_unique
        ]
        raise AlreadyExistsError(
            f"table '{table.spec.slug}': a unique value is already taken ({', '.join([*unique, 'external_ref'])})",
            details={"table": table.spec.slug, "unique": unique},
        ) from exc


async def current_table(session: AsyncSession, table: ResolvedTable) -> ResolvedTable:
    await schema_shared(session, table.namespace)
    if await catalog.spec_version(session, table.id) == table.version:
        return table
    return await catalog.load_table(session, table.namespace, table.spec.slug)


async def create(
    session: AsyncSession,
    table: ResolvedTable,
    values: Sequence[Mapping[str, object]],
    external_refs: Sequence[ExternalRef | None],
) -> list[Row]:
    table = await current_table(session, table)
    if len(external_refs) != len(values):
        raise BadRequestError("one external_ref per row")
    if table.spec.extends is not None and any(r is None for r in external_refs):
        raise BadRequestError(
            f"table '{table.spec.slug}' extends {table.spec.extends}: "
            "every row needs the external_ref of the host row it extends"
        )
    created = [
        GridRow(table_id=table.id, external_ref=external_ref_text(r), payload=stored_payload(table, v, creating=True))
        for v, r in zip(values, external_refs, strict=True)
    ]
    async with _naming_unique_columns(table), atomic(session):
        await ensure_targets_live(session, table, [row.payload for row in created])
        session.add_all(created)
        await session.flush()
    return [to_row(table, row) for row in created]


async def patch(session: AsyncSession, table: ResolvedTable, row_id: uuid.UUID, values: Mapping[str, object]) -> Row:
    return await _patch(session, await current_table(session, table), row_id, values)


async def _patch(session: AsyncSession, table: ResolvedTable, row_id: uuid.UUID, values: Mapping[str, object]) -> Row:
    row = await load_row(session, table.id, row_id, lock=True)
    changes = stored_payload(table, values, creating=False)
    async with _naming_unique_columns(table), atomic(session):
        await ensure_targets_live(session, table, [changes])
        row.payload = {**row.payload, **changes}
        await session.flush()
    return to_row(table, row)


async def upsert(
    session: AsyncSession, table: ResolvedTable, external_ref: ExternalRef, values: Mapping[str, object]
) -> Row:
    table = await current_table(session, table)
    key = external_ref_text(external_ref) or ""
    found = await load_by_external_ref(session, table.id, [key], lock=True)
    if not found:
        payload = stored_payload(table, values, creating=True)
        async with _naming_unique_columns(table), atomic(session):
            await ensure_targets_live(session, table, [payload])
            inserted = await session.scalar(
                pg_insert(GridRow)
                .values(table_id=table.id, external_ref=key, payload=payload)
                .on_conflict_do_nothing(
                    index_elements=[GridRow.table_id, GridRow.external_ref],
                    index_where=sa.and_(GridRow.external_ref.is_not(None), GridRow.archived_at.is_(None)),
                )
                .returning(GridRow.id)
            )
            if inserted is not None:
                return to_row(table, await load_row(session, table.id, inserted))
        found = await load_by_external_ref(session, table.id, [key], lock=True)
    return await _patch(session, table, found[0].id, values)


async def archive(session: AsyncSession, table: ResolvedTable, row_id: uuid.UUID) -> None:
    table = await current_table(session, table)
    row = await load_row(session, table.id, row_id, lock=True)
    plan = ArchivePlan()
    await plan.add(session, table.namespace, table.spec.slug, row)
    archived = {str(i) for i in plan.archive}
    async with atomic(session):
        for p, key, many in plan.clear:
            if p.id not in plan.archive:
                current = p.payload.get(key)
                kept = [v for v in current if v not in archived] if many and isinstance(current, list) else None
                p.payload = {**p.payload, key: kept}
        at = now()
        for _, r in plan.archive.values():
            r.archived_at = at
        await session.flush()


__all__ = ["archive", "create", "current_table", "patch", "upsert"]
