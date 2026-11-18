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
from collections.abc import Mapping, Sequence

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_grid.domain.resolved import ResolvedTable
from forktex_grid.domain.row import ExternalRef, Row, external_ref_text
from forktex_grid.errors import NotFoundError
from forktex_grid.persist.models import GridRow, GridTable


def live(table_id: uuid.UUID) -> tuple[sa.ColumnElement[bool], ...]:
    return GridRow.table_id == table_id, GridRow.archived_at.is_(None)


def locked(stmt: sa.Select[GridRow], lock: bool) -> sa.Select[GridRow]:
    return stmt.with_for_update().execution_options(populate_existing=True) if lock else stmt


def to_row(
    table: ResolvedTable,
    orm: GridRow,
    *,
    columns: Sequence[str] | None = None,
    links: Mapping[str, Row | tuple[Row, ...] | None] | None = None,
) -> Row:
    shown = table.columns.keys() if columns is None else set(columns)
    return Row(
        id=orm.id,
        external_ref=orm.external_ref,
        values={k: v for k, v in orm.payload.items() if k in shown},
        created_at=orm.created_at,
        updated_at=orm.updated_at,
        links=dict(links or {}),
    )


async def load_row(session: AsyncSession, table_id: uuid.UUID, row_id: uuid.UUID, *, lock: bool = False) -> GridRow:
    stmt = sa.select(GridRow).where(GridRow.id == row_id, *live(table_id))
    row = await session.scalar(locked(stmt, lock))
    if row is None:
        raise NotFoundError("row not found", details={"row_id": str(row_id)})
    return row


async def live_ids(session: AsyncSession, table_id: uuid.UUID, ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
    if not ids:
        return set()
    stmt = sa.select(GridRow.id).where(GridRow.id.in_(list(ids)), *live(table_id)).with_for_update(read=True)
    return set(await session.scalars(stmt))


async def load_by_external_ref(
    session: AsyncSession, table_id: uuid.UUID, refs: Sequence[str], *, lock: bool = False
) -> list[GridRow]:
    if not refs:
        return []
    stmt = sa.select(GridRow).where(GridRow.external_ref.in_(list(refs)), *live(table_id))
    return list(await session.scalars(locked(stmt, lock)))


async def get(session: AsyncSession, table: ResolvedTable, row_id: uuid.UUID) -> Row:
    return to_row(table, await load_row(session, table.id, row_id))


async def by_external_ref(
    session: AsyncSession, table: ResolvedTable, external_refs: Sequence[ExternalRef]
) -> dict[str, Row]:
    keys = sorted({external_ref_text(r) or "" for r in external_refs})
    found = await load_by_external_ref(session, table.id, keys)
    return {r.external_ref: to_row(table, r) for r in found if r.external_ref}


async def row_counts(session: AsyncSession, namespace: str) -> dict[str, int]:
    joined = sa.and_(GridRow.table_id == GridTable.id, GridRow.archived_at.is_(None))
    stmt = (
        sa.select(GridTable.slug, sa.func.count(GridRow.id))
        .select_from(GridTable)
        .outerjoin(GridRow, joined)
        .where(GridTable.namespace == namespace, GridTable.archived_at.is_(None))
        .group_by(GridTable.slug)
        .order_by(GridTable.slug)
    )
    return {slug: count for slug, count in (await session.execute(stmt)).all()}


__all__ = [
    "by_external_ref",
    "get",
    "live",
    "live_ids",
    "load_by_external_ref",
    "load_row",
    "locked",
    "row_counts",
    "to_row",
]
