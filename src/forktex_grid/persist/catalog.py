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

import sqlalchemy as sa
from forktex.database.integrity import integrity_boundary
from forktex.iso import now
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_grid.domain.enums import Cardinality, FieldType
from forktex_grid.domain.resolved import ResolvedTable, SpecVersion
from forktex_grid.domain.schema import ColumnSpec, IndexSpec, Schema, TableSpec
from forktex_grid.errors import BadRequestError, NotFoundError
from forktex_grid.persist.models import GridColumn, GridRow, GridTable


def _column_spec(orm: GridColumn) -> ColumnSpec:
    return ColumnSpec(
        key=orm.key,
        label=orm.label,
        type_id=orm.type_id,
        cardinality=Cardinality(orm.cardinality),
        is_required=orm.is_required,
        is_unique=orm.is_unique,
        config=orm.config,
        default_value=orm.default_value,
        display_order=orm.display_order,
    )


def _table_spec(orm: GridTable, columns: list[GridColumn]) -> TableSpec:
    return TableSpec(
        slug=orm.slug,
        label=orm.label,
        extends=orm.extends,
        columns=tuple(_column_spec(c) for c in columns),
        indexes=tuple(IndexSpec.model_validate(i) for i in orm.indexes),
    )


def _live_tables(namespace: str) -> sa.Select[GridTable]:
    return sa.select(GridTable).where(GridTable.namespace == namespace, GridTable.archived_at.is_(None))


async def _columns_by_table(session: AsyncSession, table_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[GridColumn]]:
    out: dict[uuid.UUID, list[GridColumn]] = defaultdict(list)
    if table_ids:
        for col in await session.scalars(
            sa.select(GridColumn)
            .where(GridColumn.table_id.in_(table_ids), GridColumn.archived_at.is_(None))
            .order_by(GridColumn.display_order, GridColumn.created_at, GridColumn.id)
        ):
            out[col.table_id].append(col)
    return out


async def hydrate(session: AsyncSession, namespace: str) -> Schema:
    tables = list(await session.scalars(_live_tables(namespace).order_by(GridTable.slug)))
    columns = await _columns_by_table(session, [t.id for t in tables])
    return Schema(tables=tuple(_table_spec(t, columns[t.id]) for t in tables))


async def table_ids(session: AsyncSession, namespace: str) -> dict[str, uuid.UUID]:
    rows = await session.execute(
        sa.select(GridTable.slug, GridTable.id).where(GridTable.namespace == namespace, GridTable.archived_at.is_(None))
    )
    return {slug: table_id for slug, table_id in rows}


async def _table_orm(session: AsyncSession, namespace: str, slug: str) -> GridTable:
    orm = await session.scalar(_live_tables(namespace).where(GridTable.slug == slug))
    if orm is None:
        raise NotFoundError(f"table '{slug}' not found", details={"table": slug})
    return orm


async def spec_version(session: AsyncSession, table_id: uuid.UUID) -> SpecVersion:
    of_table = GridColumn.table_id == table_id
    newest = sa.select(sa.func.max(GridColumn.updated_at)).where(of_table).scalar_subquery()
    count = sa.select(sa.func.count()).select_from(GridColumn).where(of_table).scalar_subquery()
    stmt = sa.select(GridTable.updated_at, GridTable.archived_at, newest, count).where(GridTable.id == table_id)
    row = (await session.execute(stmt)).one_or_none()
    return tuple(row) if row is not None else ()


async def load_table(session: AsyncSession, namespace: str, slug: str) -> ResolvedTable:
    orm = await _table_orm(session, namespace, slug)
    columns = await _columns_by_table(session, [orm.id])
    version = await spec_version(session, orm.id)
    return ResolvedTable(orm.id, namespace, _table_spec(orm, columns[orm.id]), version)


def _column_orm(table_id: uuid.UUID, spec: ColumnSpec) -> GridColumn:
    return GridColumn(
        table_id=table_id,
        key=spec.key,
        label=spec.label,
        type_id=spec.type_id,
        cardinality=spec.cardinality,
        is_required=spec.is_required,
        is_unique=spec.is_unique,
        config=spec.config,
        default_value=spec.default_value,
        display_order=spec.display_order,
    )


async def create_table(session: AsyncSession, namespace: str, spec: TableSpec) -> uuid.UUID:
    orm = GridTable(
        namespace=namespace,
        slug=spec.slug,
        label=spec.label,
        extends=spec.extends,
        indexes=[i.model_dump(mode="json") for i in spec.indexes],
    )
    session.add(orm)
    async with integrity_boundary():
        await session.flush()
        session.add_all(_column_orm(orm.id, c) for c in spec.columns)
        await session.flush()
    return orm.id


async def alter_table(session: AsyncSession, namespace: str, spec: TableSpec) -> None:
    orm = await _table_orm(session, namespace, spec.slug)
    if orm.extends != spec.extends:
        raise BadRequestError(f"table '{spec.slug}' cannot change what it extends; drop it and create it again")
    orm.label = spec.label
    await session.flush()


async def set_indexes(session: AsyncSession, namespace: str, spec: TableSpec) -> None:
    orm = await _table_orm(session, namespace, spec.slug)
    orm.indexes = [i.model_dump(mode="json") for i in spec.indexes]
    await session.flush()


async def archive_table(session: AsyncSession, namespace: str, slug: str) -> uuid.UUID:
    orm = await _table_orm(session, namespace, slug)
    at = now()
    for model in (GridColumn, GridRow):
        await session.execute(
            sa.update(model).where(model.table_id == orm.id, model.archived_at.is_(None)).values(archived_at=at)
        )
    orm.archived_at = at
    await session.flush()
    return orm.id


async def _column(session: AsyncSession, table_id: uuid.UUID, key: str) -> GridColumn:
    orm = await session.scalar(
        sa.select(GridColumn).where(
            GridColumn.table_id == table_id, GridColumn.key == key, GridColumn.archived_at.is_(None)
        )
    )
    if orm is None:
        raise NotFoundError(f"column '{key}' not found", details={"column": key})
    return orm


async def add_column(session: AsyncSession, table_id: uuid.UUID, spec: ColumnSpec) -> None:
    await session.execute(
        sa.update(GridRow)
        .where(GridRow.table_id == table_id, GridRow.payload.has_key(spec.key))
        .values(payload=GridRow.payload.op("-")(spec.key))
    )
    session.add(_column_orm(table_id, spec))
    async with integrity_boundary():
        await session.flush()


async def alter_column(session: AsyncSession, table_id: uuid.UUID, spec: ColumnSpec) -> None:
    orm = await _column(session, table_id, spec.key)
    if (orm.type_id, orm.cardinality) != (spec.type_id, spec.cardinality):
        raise BadRequestError(
            f"column '{spec.key}' cannot change its type or cardinality in place; "
            "add a new column and copy the values, or reshape the namespace with migrate_to"
        )
    if orm.type_id == FieldType.ref and (orm.config or {}).get("table") != spec.config.get("table"):
        raise BadRequestError(
            f"column '{spec.key}' cannot change which table it links to in place: its values name rows of "
            f"'{(orm.config or {}).get('table')}'; add a new link column, or reshape the namespace with migrate_to"
        )
    for field in ("label", "is_required", "is_unique", "config", "default_value", "display_order"):
        setattr(orm, field, getattr(spec, field))
    await session.flush()


async def archive_column(session: AsyncSession, table_id: uuid.UUID, key: str) -> None:
    (await _column(session, table_id, key)).archived_at = now()
    await session.flush()


async def rename_column(session: AsyncSession, table_id: uuid.UUID, key: str, new_key: str) -> None:
    orm = await _column(session, table_id, key)
    ColumnSpec.model_validate({**_column_spec(orm).model_dump(), "key": new_key})
    payload = GridRow.payload
    await session.execute(
        sa.update(GridRow)
        .where(GridRow.table_id == table_id, payload.has_key(new_key))
        .values(payload=payload.op("-")(new_key))
    )
    await session.execute(
        sa.update(GridRow)
        .where(GridRow.table_id == table_id, GridRow.archived_at.is_(None), payload.has_key(key))
        .values(payload=payload.op("-")(key).op("||")(sa.func.jsonb_build_object(new_key, payload[key])))
    )
    orm.key = new_key
    table = await session.get(GridTable, table_id)
    if table is not None and table.indexes:
        renamed = []
        for stored in table.indexes:
            index = IndexSpec.model_validate(stored)
            keys = tuple(new_key if k == key else k for k in index.column_keys)
            renamed.append(index.model_copy(update={"column_keys": keys}).model_dump(mode="json"))
        table.indexes = renamed
    async with integrity_boundary():
        await session.flush()


__all__ = [
    "add_column",
    "alter_column",
    "alter_table",
    "archive_column",
    "archive_table",
    "create_table",
    "hydrate",
    "load_table",
    "rename_column",
    "set_indexes",
    "spec_version",
    "table_ids",
]
