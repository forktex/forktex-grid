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
from collections import defaultdict
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime
from typing import Any, cast

import sqlalchemy as sa
from forktex.iso import to_iso
from forktex.types import BaseWireValueObject, JsonObject, JsonValue
from pydantic import Field
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_grid.domain.builtin_types import RefConfig
from forktex_grid.domain.enums import Cardinality, FieldType
from forktex_grid.domain.migration import Migration, TableSource
from forktex_grid.domain.query import SYSTEM_FIELDS
from forktex_grid.domain.resolved import ResolvedTable
from forktex_grid.domain.schema import TableSpec
from forktex_grid.domain.values import stored_payload
from forktex_grid.errors import AlreadyExistsError, BadRequestError
from forktex_grid.persist import catalog
from forktex_grid.persist.locks import schema_exclusive
from forktex_grid.persist.models import GridRow
from forktex_grid.persist.tx import atomic, rehearsal
from forktex_grid.read.rows import live, row_counts
from forktex_grid.write.apply import ApplyOptions, ApplyReport, apply_schema, in_one_transaction, lock_namespace

CHUNK = 1000
_ROWS = cast(sa.Table, GridRow.__table__)

type SourceRow = sa.Row[uuid.UUID, str | None, JsonObject, datetime, datetime]


class MigratedTable(BaseWireValueObject):
    table: str
    sources: tuple[str, ...]
    rows: int
    columns_not_carried: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    links_dropped: int = 0


class MigrationReport(BaseWireValueObject):
    source: str
    target: str
    schema_report: ApplyReport
    tables: tuple[MigratedTable, ...]
    tables_not_carried: tuple[str, ...] = ()
    dry_run: bool = False


def _links(spec: TableSpec) -> dict[str, tuple[str, bool]]:
    return {
        c.key: (RefConfig.model_validate(c.config).table, c.cardinality is Cardinality.many)
        for c in spec.columns
        if c.type_id == FieldType.ref
    }


def _links_between(specs: Mapping[str, TableSpec], *, required: bool = False) -> dict[str, list[str]]:
    edges: dict[str, list[str]] = {}
    for slug, spec in specs.items():
        targets = {
            RefConfig.model_validate(c.config).table
            for c in spec.columns
            if c.type_id == FieldType.ref and (c.is_required or not required)
        }
        edges[slug] = sorted(targets & specs.keys())
    return edges


def _components(edges: Mapping[str, list[str]]) -> list[list[str]]:
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    found: list[list[str]] = []

    def visit(slug: str) -> None:
        index[slug] = low[slug] = len(index)
        stack.append(slug)
        for target in edges[slug]:
            if target not in index:
                visit(target)
                low[slug] = min(low[slug], low[target])
            elif target in stack:
                low[slug] = min(low[slug], index[target])
        if low[slug] == index[slug]:
            component: list[str] = []
            while not component or component[-1] != slug:
                component.append(stack.pop())
            found.append(sorted(component))

    for slug in sorted(edges):
        if slug not in index:
            visit(slug)
    return found


def _within(component: list[str], required: Mapping[str, list[str]]) -> list[str]:
    ordered: list[str] = []
    left = list(component)
    while left:
        ready = [s for s in left if all(t in ordered or t == s or t not in component for t in required[s])]
        slug = ready[0] if ready else left[0]
        ordered.append(slug)
        left.remove(slug)
    return ordered


def _fill_order(specs: Mapping[str, TableSpec]) -> list[str]:
    required = _links_between(specs, required=True)
    return [slug for component in _components(_links_between(specs)) for slug in _within(component, required)]


def _column_map(source: TableSpec, target: TableSpec, mapping: TableSource) -> dict[str, str]:
    have = {c.key for c in source.columns}
    want = {c.key for c in target.columns}
    if mapping.columns is None:
        return {key: key for key in sorted(want & have)}
    if unknown := sorted(set(mapping.columns) - want):
        raise BadRequestError(f"table '{target.slug}' of the new schema has no column(s) {unknown}")
    if missing := sorted(set(mapping.columns.values()) - have - SYSTEM_FIELDS):
        raise BadRequestError(f"table '{source.slug}' has no column(s) {missing}")
    return dict(mapping.columns)


_TOO_MANY: Any = object()


def _shaped(value: JsonValue, many: bool) -> JsonValue:
    if many:
        return value if isinstance(value, list) else [value]
    if not isinstance(value, list):
        return value
    if len(value) > 1:
        return _TOO_MANY
    return value[0] if value else None


def _system_value(row: SourceRow, key: str) -> JsonValue:
    value = getattr(row, key)
    if value is None or isinstance(value, str):
        return value
    return str(value) if key == "id" else to_iso(value)


def _repoint(old: JsonValue, many: bool, new_ids: Mapping[str, str]) -> tuple[JsonValue, int]:
    olds = [str(v) for v in old] if many and isinstance(old, list) else [str(old)]
    found: list[JsonValue] = [new_ids[o] for o in olds if o in new_ids]
    kept: JsonValue = found if many else (found[0] if found else None)
    return kept, len(olds) - len(found)


async def _rows_of(session: AsyncSession, table_id: uuid.UUID) -> AsyncIterator[Sequence[SourceRow]]:
    columns = (GridRow.id, GridRow.external_ref, GridRow.payload, GridRow.created_at, GridRow.updated_at)
    after: uuid.UUID | None = None
    while True:
        stmt = sa.select(*columns).where(*live(table_id)).order_by(GridRow.id).limit(CHUNK)
        if after is not None:
            stmt = stmt.where(GridRow.id > after)
        chunk = list((await session.execute(stmt)).all())
        if not chunk:
            return
        yield chunk
        after = chunk[-1].id


class _Copy:
    def __init__(self, session: AsyncSession, migration: Migration, sources: Mapping[str, TableSpec]) -> None:
        self.session = session
        self.migration = migration
        self.sources = sources
        self.by_target: dict[str, list[TableSource]] = defaultdict(list)
        for mapping in migration.tables:
            self.by_target[mapping.target].append(mapping)
        self.new_ids: dict[str, dict[str, str]] = defaultdict(dict)
        self.deferred: list[dict[str, Any]] = []
        self.links: dict[str, dict[str, tuple[str, bool]]] = {t.slug: _links(t) for t in migration.desired.tables}

    async def fill(
        self, target: ResolvedTable, filled_before: set[str], source_ids: Mapping[str, uuid.UUID]
    ) -> MigratedTable:
        links = self.links[target.spec.slug]
        dropped = 0
        rows = 0
        not_carried: dict[str, tuple[str, ...]] = {}
        for mapping in self.by_target[target.spec.slug]:
            source = self.sources[mapping.source]
            columns = _column_map(source, target.spec, mapping)
            listed = {c.key: c.cardinality is Cardinality.many for c in source.columns}
            reshaped = frozenset(
                new
                for new, old in columns.items()
                if listed.get(old, False) != (target.column(new).spec.cardinality is Cardinality.many)
            )
            if lost := tuple(sorted({c.key for c in source.columns} - set(columns.values()))):
                not_carried[mapping.source] = lost
            async for chunk in _rows_of(self.session, source_ids[mapping.source]):
                inserts: list[dict[str, Any]] = []
                for row in chunk:
                    values, pending, lost_links = self._values(
                        row, columns, reshaped, mapping, target, links, filled_before
                    )
                    dropped += lost_links
                    inserts.append(self._insert(target, mapping, row, values))
                    self.new_ids[target.spec.slug][str(row.id)] = str(inserts[-1]["id"])
                    if pending:
                        self.deferred.append({"rid": inserts[-1]["id"], "links": pending, "table": target.spec.slug})
                await self._write(target, inserts)
                rows += len(inserts)
        return MigratedTable(
            table=target.spec.slug,
            sources=tuple(m.source for m in self.by_target[target.spec.slug]),
            rows=rows,
            columns_not_carried=not_carried,
            links_dropped=dropped,
        )

    def _insert(
        self, target: ResolvedTable, mapping: TableSource, row: SourceRow, values: dict[str, JsonValue]
    ) -> dict[str, Any]:
        if target.spec.extends is not None and row.external_ref is None:
            raise BadRequestError(
                f"row {row.id} of '{mapping.source}' has no external_ref, and '{target.spec.slug}' extends "
                f"{target.spec.extends}: every row needs the key of the host row it extends"
            )
        try:
            payload = stored_payload(target, values, creating=True)
        except BadRequestError as exc:
            raise BadRequestError(
                f"row {row.id} of '{mapping.source}' does not fit '{target.spec.slug}': {exc}",
                details={"source": mapping.source, "row_id": str(row.id)},
            ) from None
        return {
            "id": uuid.uuid7(),
            "table_id": target.id,
            "external_ref": row.external_ref,
            "payload": payload,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    async def _write(self, target: ResolvedTable, inserts: list[dict[str, Any]]) -> None:
        try:
            async with atomic(self.session):
                await self.session.execute(sa.insert(_ROWS), inserts)
        except AlreadyExistsError:
            raise AlreadyExistsError(
                f"table '{target.spec.slug}': two copied rows hold the same unique value or external_ref",
                details={"table": target.spec.slug},
            ) from None

    def _values(
        self,
        row: SourceRow,
        columns: Mapping[str, str],
        reshaped: frozenset[str],
        mapping: TableSource,
        target: ResolvedTable,
        links: Mapping[str, tuple[str, bool]],
        filled_before: set[str],
    ) -> tuple[dict[str, JsonValue], dict[str, JsonValue], int]:
        values: dict[str, JsonValue] = {}
        for new_key, old_key in columns.items():
            value = _system_value(row, old_key) if old_key in SYSTEM_FIELDS else row.payload.get(old_key)
            if value is not None:
                values[new_key] = value
        for key in reshaped & values.keys():
            shaped = _shaped(values[key], target.column(key).spec.cardinality is Cardinality.many)
            if shaped is _TOO_MANY:
                raise BadRequestError(
                    f"row {row.id} of '{mapping.source}' holds several values for '{target.spec.slug}.{key}', "
                    "which holds one",
                    details={"source": mapping.source, "row_id": str(row.id), "column": key},
                )
            if shaped is None:
                values.pop(key)
            else:
                values[key] = shaped
        values |= mapping.constants
        pending: dict[str, JsonValue] = {}
        dropped = 0
        for key, (points_at, many) in links.items():
            if key not in values:
                continue
            if points_at not in self.by_target:
                gone = values.pop(key)
                dropped += len(gone) if isinstance(gone, list) else 1
                continue
            if points_at not in filled_before:
                if target.column(key).spec.is_required:
                    raise BadRequestError(
                        f"'{target.spec.slug}.{key}' is a required link inside a cycle of links: make it optional "
                        "in the new schema, migrate, then require it"
                    )
                pending[key] = values.pop(key)
                continue
            repointed, lost = _repoint(values[key], many, self.new_ids[points_at])
            dropped += lost
            if repointed in (None, []):
                values.pop(key)
            else:
                values[key] = repointed
        return values, pending, dropped

    async def finish(self) -> dict[str, int]:
        dropped: dict[str, int] = defaultdict(int)
        updates: list[dict[str, Any]] = []
        for deferred in self.deferred:
            slug: str = deferred["table"]
            values: dict[str, JsonValue] = {}
            for key, old in deferred["links"].items():
                points_at, many = self.links[slug][key]
                repointed, lost = _repoint(old, many, self.new_ids[points_at])
                dropped[slug] += lost
                if repointed not in (None, []):
                    values[key] = repointed
            if values:
                updates.append({"rid": deferred["rid"], "links": values})
        statement = (
            sa.update(_ROWS)
            .where(_ROWS.c.id == sa.bindparam("rid"))
            .values(payload=_ROWS.c.payload.op("||")(sa.bindparam("links", type_=JSONB)), updated_at=_ROWS.c.updated_at)
        )
        for i in range(0, len(updates), CHUNK):
            await self.session.execute(statement, updates[i : i + CHUNK])
        return dropped


async def _migrate(
    session: AsyncSession, source: str, target: str, migration: Migration, options: ApplyOptions
) -> MigrationReport:
    sources = {t.slug: t for t in (await catalog.hydrate(session, source)).tables}
    if missing := sorted({m.source for m in migration.tables} - set(sources)):
        raise BadRequestError(f"namespace '{source}' has no table(s) {missing}")
    source_ids = await catalog.table_ids(session, source)
    async with atomic(session):
        schema_report = await apply_schema(session, target, migration.desired, options)
        copy = _Copy(session, migration, sources)
        specs = {t.slug: t for t in migration.desired.tables}
        order = _fill_order({slug: specs[slug] for slug in copy.by_target})
        tables: list[MigratedTable] = []
        for i, slug in enumerate(order):
            resolved = await catalog.load_table(session, target, slug)
            tables.append(await copy.fill(resolved, set(order[:i]), source_ids))
        late_drops = await copy.finish()
        counted = await row_counts(session, target)
    for table in tables:
        if counted.get(table.table) != table.rows:
            raise BadRequestError(f"'{table.table}' holds {counted.get(table.table)} rows after copying {table.rows}")
    tables = [t.model_copy(update={"links_dropped": t.links_dropped + late_drops.get(t.table, 0)}) for t in tables]
    return MigrationReport(
        source=source,
        target=target,
        schema_report=schema_report,
        tables=tuple(tables),
        tables_not_carried=tuple(sorted(set(sources) - {m.source for m in migration.tables})),
    )


async def migrate(
    session: AsyncSession, source: str, target: str, migration: Migration, options: ApplyOptions
) -> MigrationReport:
    options = in_one_transaction(options, "a migration")
    if source == target:
        raise BadRequestError("a migration copies into another namespace")
    if not options.dry_run:
        return await _locked(session, source, target, migration, options)
    async with rehearsal(session):
        report = await _locked(session, source, target, migration, options.model_copy(update={"dry_run": False}))
    schema_report = report.schema_report.model_copy(update={"options": options})
    return report.model_copy(update={"dry_run": True, "schema_report": schema_report})


async def _locked(
    session: AsyncSession, source: str, target: str, migration: Migration, options: ApplyOptions
) -> MigrationReport:
    await lock_namespace(session, target)
    for namespace in sorted((source, target)):
        await schema_exclusive(session, namespace)
    if await catalog.table_ids(session, target):
        raise BadRequestError(f"namespace '{target}' already has tables; a migration fills an empty namespace")
    return await _migrate(session, source, target, migration, options)


__all__ = ["MigratedTable", "MigrationReport", "migrate"]
