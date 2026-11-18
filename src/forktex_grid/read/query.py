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
from collections.abc import Sequence

import sqlalchemy as sa
from forktex.database import savepoint
from forktex.database.filters import FilterOp
from forktex.database.integrity import read_boundary
from forktex.database.pagination import decode_cursor
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_grid.domain.enums import Cardinality
from forktex_grid.domain.query import SYSTEM_FIELDS, CursorPaging, Embed, Query, Where
from forktex_grid.domain.resolved import ResolvedTable
from forktex_grid.domain.row import Page, Row, ValueCount, ValueCounts
from forktex_grid.domain.values import linked_ids
from forktex_grid.errors import BadRequestError
from forktex_grid.persist import catalog
from forktex_grid.persist.extensions import require_trgm
from forktex_grid.persist.models import GridRow
from forktex_grid.read.compile import (
    SortOrder,
    after_cursor,
    compile_where,
    cursor_of,
    leading_bound,
    links_in,
    null_tail,
    order_by,
    ref_target,
    sort_order,
    uses_fuzzy,
)
from forktex_grid.read.rows import live, to_row

MAX_PAGE_SIZE = 1000
MAX_OFFSET = 100_000
MAX_GROUPS = 1000


def _check_paging(query: Query, order: SortOrder) -> None:
    page = query.page
    if not 1 <= page.size <= MAX_PAGE_SIZE:
        raise BadRequestError(f"page size must be between 1 and {MAX_PAGE_SIZE}")
    if isinstance(page, CursorPaging):
        if len({desc for _, desc in order}) > 1:
            raise BadRequestError("cursor paging needs every sort key in one direction")
    elif not 0 <= page.offset <= MAX_OFFSET:
        raise BadRequestError(f"offset must be between 0 and {MAX_OFFSET}")


def _check_columns(table: ResolvedTable, columns: Sequence[str] | None) -> None:
    for key in columns or ():
        if key not in SYSTEM_FIELDS:
            table.column(key)


async def _link_targets(
    session: AsyncSession, table: ResolvedTable, where: Where | None, embeds: frozenset[str] = frozenset()
) -> dict[str, ResolvedTable]:
    if uses_fuzzy(where):
        await require_trgm(session, "the 'fuzzy' filter")
    names = set(links_in(where)) | embeds
    targets: dict[str, ResolvedTable] = {}
    for name in sorted(names):
        _, slug = ref_target(table, name)
        targets[name] = await catalog.load_table(session, table.namespace, slug)
    return targets


def scope(table: ResolvedTable, where: Where | None, targets: dict[str, ResolvedTable]) -> list[sa.ColumnElement[bool]]:
    conditions = list(live(table.id))
    if where is not None:
        conditions.append(compile_where(where, table, targets))
    return conditions


async def _embedded(
    session: AsyncSession, table: ResolvedTable, embed: Embed, target: ResolvedTable, found: list[GridRow]
) -> dict[uuid.UUID, Row | tuple[Row, ...] | None]:
    column = table.column(embed.link)
    many = column.spec.cardinality is Cardinality.many

    ids = {target_id for row in found for target_id in linked_ids(row.payload.get(embed.link))}
    loaded: dict[uuid.UUID, Row] = {}
    if ids:
        stmt = sa.select(GridRow).where(GridRow.id.in_(ids), *live(target.id))
        loaded = {r.id: to_row(target, r, columns=embed.select) for r in await session.scalars(stmt)}
    out: dict[uuid.UUID, Row | tuple[Row, ...] | None] = {}
    for row in found:
        rows = [loaded[i] for i in linked_ids(row.payload.get(embed.link)) if i in loaded]
        out[row.id] = tuple(rows) if many else (rows[0] if rows else None)
    return out


async def run_query(session: AsyncSession, table: ResolvedTable, query: Query) -> Page:
    order = sort_order(table, query.order_by)
    _check_paging(query, order)
    _check_columns(table, query.columns)
    targets = await _link_targets(session, table, query.where, frozenset(e.link for e in query.embeds))
    for embed in query.embeds:
        _check_columns(targets[embed.link], embed.select)

    page = query.page
    keyset = isinstance(page, CursorPaging)
    conditions = scope(table, query.where, targets)
    ordered = order_by(sa.select(GridRow).where(*conditions), order)
    stmt = ordered.limit(page.size + 1)
    tail: sa.Select[GridRow] | None = None
    if isinstance(page, CursorPaging):
        if page.after is not None:
            boundary = decode_cursor(page.after, expected_length=len(order) + 1)
            stmt = stmt.where(after_cursor(order, boundary))
            if (bound := leading_bound(order, boundary)) is not None:
                stmt = stmt.where(bound)
            if (nulls := null_tail(order, boundary)) is not None:
                tail = ordered.where(nulls)
    else:
        stmt = stmt.offset(page.offset)

    async with read_boundary(), savepoint(session):
        found = list(await session.scalars(stmt))
        if tail is not None and len(found) <= page.size:
            found += list(await session.scalars(tail.limit(page.size + 1 - len(found))))
        total = await session.scalar(sa.select(sa.func.count()).where(*conditions)) if query.count else None
        shown = found[: page.size]
        embedded = {e.link: await _embedded(session, table, e, targets[e.link], shown) for e in query.embeds}

    has_more = len(found) > page.size
    rows = [
        to_row(table, r, columns=query.columns, links={name: by_row[r.id] for name, by_row in embedded.items()})
        for r in shown
    ]
    next_cursor = cursor_of(shown[-1], order) if keyset and has_more else None
    return Page(items=rows, has_more=has_more, next_cursor=next_cursor, total=total)


async def count_rows(session: AsyncSession, table: ResolvedTable, where: Where | None) -> int:
    targets = await _link_targets(session, table, where)
    async with read_boundary(), savepoint(session):
        return await session.scalar(sa.select(sa.func.count()).where(*scope(table, where, targets))) or 0


_JSON_NULL = sa.literal_column("'null'::jsonb", JSONB)
_EMPTY_LIST = sa.literal_column("'[]'::jsonb", JSONB)


async def count_by(
    session: AsyncSession, table: ResolvedTable, column: str, where: Where | None, limit: int
) -> ValueCounts:
    if not 1 <= limit <= MAX_GROUPS:
        raise BadRequestError(f"limit must be between 1 and {MAX_GROUPS}")
    resolved = table.column(column)
    if FilterOp.eq not in resolved.handler.capabilities.filter_ops:
        raise BadRequestError(f"column '{column}' ({resolved.handler.type_id}) has no equality to group by")
    targets = await _link_targets(session, table, where)
    conditions = scope(table, where, targets)
    stored = GridRow.payload[column]
    if resolved.spec.cardinality is Cardinality.many:
        listed = sa.case((sa.func.jsonb_typeof(stored) == "array", stored), else_=_EMPTY_LIST)
        elements = sa.func.jsonb_array_elements(listed).table_valued(sa.column("value", JSONB)).lateral()
        source = sa.select(elements.c.value.label("value")).select_from(GridRow).join(elements, sa.true())
        source = source.where(*conditions).subquery()
    else:
        value = sa.func.nullif(stored, _JSON_NULL)
        source = sa.select(value.label("value")).where(*conditions).subquery()
    n = sa.func.count().label("n")
    groups = (
        sa.select(source.c.value, n)
        .group_by(source.c.value)
        .order_by(n.desc(), source.c.value.asc().nulls_last())
        .limit(limit)
    )
    async with read_boundary(), savepoint(session):
        found = [ValueCount(value=value, count=count) for value, count in (await session.execute(groups)).all()]
        total = await session.scalar(sa.select(sa.func.count()).select_from(source)) or 0
    return ValueCounts(groups=tuple(found), others=total - sum(g.count for g in found))


__all__ = ["MAX_GROUPS", "MAX_OFFSET", "MAX_PAGE_SIZE", "count_by", "count_rows", "run_query"]
