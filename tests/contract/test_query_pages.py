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

from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_grid import BadRequestError, CursorPaging, Namespace, OffsetPaging, Query, SortKey, Table, col
from tests.conftest import declare
from tests.contract.query_tables import THINGS, things


@pytest.mark.parametrize("column", ["tags", "data", "code"])
async def test_a_column_that_cannot_be_sorted_is_refused(session: AsyncSession, column: str) -> None:
    table = await things(session)
    with pytest.raises(BadRequestError):
        await table.query(Query(order_by=(SortKey(column=column),)))


async def test_nulls_sort_last_ascending_and_first_descending(session: AsyncSession) -> None:
    table = await things(session)
    asc = await table.query(Query(order_by=(col("count").asc(),)))
    desc = await table.query(Query(order_by=(col("count").desc(),)))
    assert [r.get("count") for r in asc.items] == [1, 10, 100, None]
    assert [r.get("count") for r in desc.items] == [None, 100, 10, 1]


async def test_rows_with_equal_sort_keys_are_ordered_by_id(session: AsyncSession) -> None:
    (table,) = await declare(Namespace(session, "acme"), THINGS)
    created = await table.create_many([{"kind": "a"} for _ in range(4)])
    page = await table.query(Query(order_by=(col("kind").asc(),)))
    assert [r.id for r in page.items] == sorted(r.id for r in created)


async def test_offset_pages_report_has_more_exactly_and_count_on_request(session: AsyncSession) -> None:
    (table,) = await declare(Namespace(session, "acme"), THINGS)
    await table.create_many([{"count": i} for i in range(4)])
    by_count = (col("count").asc(),)
    first = await table.query(Query(order_by=by_count, page=OffsetPaging(size=2), count=True))
    last = await table.query(Query(order_by=by_count, page=OffsetPaging(size=2, offset=2)))
    assert [r["count"] for r in first.items] == [0, 1] and first.has_more and first.total == 4
    assert [r["count"] for r in last.items] == [2, 3] and not last.has_more and last.total is None


async def _walk(table: Table, direction: str) -> list[Any]:
    seen: list[Any] = []
    after = None
    while True:
        query = Query.model_validate(
            {
                "orderBy": [{"column": "count", "direction": direction}],
                "page": {"by": "cursor", "size": 2, "after": after},
            }
        )
        page = await table.query(query)
        seen += [r.id for r in page.items]
        if not page.has_more:
            return seen
        assert page.next_cursor is not None
        after = page.next_cursor


@pytest.mark.parametrize("direction", ["asc", "desc"])
async def test_cursor_pages_walk_every_row_once_in_order(session: AsyncSession, direction: str) -> None:
    (table,) = await declare(Namespace(session, "acme"), THINGS)
    await table.create_many([{"count": c} for c in (3, 1, None, 2, 1, None, 3)])
    order = (SortKey.model_validate({"column": "count", "direction": direction}),)
    everything = await table.query(Query(order_by=order, page=OffsetPaging(size=100)))
    walked = await _walk(table, direction)
    assert sorted(walked) == sorted(r.id for r in everything.items) and len(walked) == 7
    counts = {r.id: r.get("count") for r in everything.items}
    values = [counts[i] for i in walked]
    present = [v for v in values if v is not None]
    assert present == sorted(present, reverse=direction == "desc")
    assert (values[-2:] if direction == "asc" else values[:2]) == [None, None]


async def test_cursor_pages_walk_by_a_system_field(session: AsyncSession) -> None:
    (table,) = await declare(Namespace(session, "acme"), THINGS)
    created = await table.create_many([{"count": i} for i in range(5)])
    seen, after = [], None
    while True:
        page = await table.query(Query(order_by=(col("created_at").desc(),), page=CursorPaging(size=2, after=after)))
        seen += [r.id for r in page.items]
        if not page.has_more:
            break
        after = page.next_cursor
    assert sorted(seen) == sorted(r.id for r in created) and len(seen) == 5


async def test_cursor_paging_needs_one_direction(session: AsyncSession) -> None:
    table = await things(session)
    with pytest.raises(BadRequestError):
        await table.query(Query(order_by=(col("count").asc(), col("name").desc()), page=CursorPaging()))


@pytest.mark.parametrize("cursor", ["not-base64!!", "W10=", "WyJ4Il0="])
async def test_a_garbage_cursor_is_refused(session: AsyncSession, cursor: str) -> None:
    table = await things(session)
    with pytest.raises(BadRequestError):
        await table.query(Query(order_by=(col("count").asc(),), page=CursorPaging(after=cursor)))


@pytest.mark.parametrize("size", [0, 1001])
async def test_a_page_size_outside_the_bounds_is_refused(session: AsyncSession, size: int) -> None:
    table = await things(session)
    with pytest.raises(BadRequestError):
        await table.query(Query(page=OffsetPaging(size=size)))


@pytest.mark.parametrize("field", ["id", "external_ref", "created_at", "updated_at"])
async def test_every_system_field_sorts_and_pages(session: AsyncSession, field: str) -> None:
    (table,) = await declare(Namespace(session, "acme"), THINGS)
    created = await table.create_many([{"count": i} for i in range(5)], external_refs=[f"k{i}" for i in range(5)])
    seen, after = [], None
    while True:
        page = await table.query(Query(order_by=(col(field).asc(),), page=CursorPaging(size=2, after=after)))
        seen += [r.id for r in page.items]
        if not page.has_more:
            break
        after = page.next_cursor
    assert sorted(seen) == sorted(r.id for r in created) and len(seen) == 5


async def test_a_cursor_page_starts_its_scan_at_the_cursor_and_reads_nulls_only_after(session: AsyncSession) -> None:
    (table,) = await declare(Namespace(session, "acme"), THINGS)
    await table.create_many([{"count": c} for c in (1, 2, 3, None)])
    sent: list[str] = []

    def capture(conn: object, cursor: object, statement: str, *rest: object) -> None:
        if statement.lstrip().upper().startswith("SELECT") and "grid_row" in statement:
            sent.append(statement)

    engine = session.get_bind()
    first = await table.query(Query(order_by=(col("count").asc(),), page=CursorPaging(size=1)))
    event.listen(engine, "before_cursor_execute", capture)
    try:
        middle = await table.query(
            Query(order_by=(col("count").asc(),), page=CursorPaging(size=1, after=first.next_cursor))
        )
        assert len(sent) == 1 and ">=" in sent[0]
        sent.clear()
        second = await table.query(
            Query(order_by=(col("count").asc(),), page=CursorPaging(size=1, after=middle.next_cursor))
        )
        last = await table.query(
            Query(order_by=(col("count").asc(),), page=CursorPaging(size=2, after=second.next_cursor))
        )
        assert [r.get("count") for r in last.items] == [None]
        assert any("IS NULL" in s for s in sent)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
