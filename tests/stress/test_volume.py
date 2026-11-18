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
import time
from collections.abc import Iterator
from typing import Any

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_grid import ColumnSpec, CursorPaging, Namespace, OffsetPaging, Query, TableSpec, col
from tests.conftest import declare

ROWS = 5_000
CHUNK = 500

BUDGET_WRITE_S = 30.0
BUDGET_LOOKUP_S = 0.5
BUDGET_PAGE_WALK_S = 10.0

ORDERS = TableSpec(
    slug="order",
    columns=(
        ColumnSpec(key="number", type_id="text", is_unique=True),
        ColumnSpec(key="total", type_id="decimal"),
        ColumnSpec(key="region", type_id="enum", config={"options": ["north", "south", "east", "west"]}),
    ),
)
REGIONS = ["north", "south", "east", "west"]


async def test_thousands_of_rows_write_filter_and_page_within_budget(session: AsyncSession) -> None:
    (orders,) = await declare(Namespace(session, "volume"), ORDERS)

    started = time.perf_counter()
    for chunk in range(0, ROWS, CHUNK):
        await orders.create_many(
            [
                {"number": f"N-{i:06d}", "total": f"{i % 997}.{i % 100:02d}", "region": REGIONS[i % 4]}
                for i in range(chunk, chunk + CHUNK)
            ]
        )
    assert time.perf_counter() - started < BUDGET_WRITE_S
    assert await orders.count() == ROWS
    await session.execute(sa.text("ANALYZE"))

    started = time.perf_counter()
    found = await orders.query(Query(where=col("number") == "N-004242"))
    assert time.perf_counter() - started < BUDGET_LOOKUP_S
    assert [r["number"] for r in found.items] == ["N-004242"]

    assert await orders.count((col("region") == "north") & (col("total") >= 900)) == sum(
        1 for i in range(ROWS) if i % 4 == 0 and float(f"{i % 997}.{i % 100:02d}") >= 900
    )

    started = time.perf_counter()
    seen, after = 0, None
    while True:
        page = await orders.query(Query(order_by=(col("total").desc(),), page=CursorPaging(size=500, after=after)))
        seen += len(page.items)
        if not page.has_more:
            break
        after = page.next_cursor
    assert seen == ROWS and time.perf_counter() - started < BUDGET_PAGE_WALK_S


async def test_a_filter_on_a_unique_column_uses_grid_expression_index(session: AsyncSession) -> None:
    (orders,) = await declare(Namespace(session, "plan"), ORDERS)
    await orders.create_many([{"number": f"N-{i:05d}", "region": REGIONS[i % 4]} for i in range(2_000)])
    await session.execute(sa.text("ANALYZE"))
    with _captured_selects(session) as sent:
        await orders.query(Query(where=col("number") == "N-01234", page=OffsetPaging(size=5)))
    statement, parameters = sent[0]
    connection = await session.connection()
    plan = "\n".join(row[0] for row in await connection.exec_driver_sql(f"EXPLAIN {statement}", parameters))
    assert "gx_" in plan, plan


@contextlib.contextmanager
def _captured_selects(session: AsyncSession) -> Iterator[list[tuple[str, Any]]]:
    sent: list[tuple[str, Any]] = []
    engine = session.get_bind()

    def capture(conn: object, cursor: object, statement: str, parameters: Any, context: object, many: bool) -> None:
        if statement.lstrip().upper().startswith("SELECT") and "grid_row" in statement:
            sent.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield sent
    finally:
        event.remove(engine, "before_cursor_execute", capture)
