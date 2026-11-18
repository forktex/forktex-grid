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

import asyncio
import os
import time

import sqlalchemy as sa
from _consumer import Report, check, engine, session_of
from sqlalchemy import event

from forktex_grid import (
    ApplyOptions,
    ColumnSpec,
    CursorPaging,
    IndexSpec,
    Namespace,
    OffsetPaging,
    Query,
    Schema,
    TableSpec,
    apply_migrations,
    col,
    link,
)

ROWS = int(os.environ.get("BATTLE_ROWS", "100000"))
CUSTOMERS = 2000
CHUNK = 1000
REGIONS = ["north", "south", "east", "west"]

SCHEMA = Schema(
    tables=[
        TableSpec(
            slug="customer",
            columns=[
                ColumnSpec(key="name", type_id="text", is_unique=True),
                ColumnSpec(key="region", type_id="enum", config={"options": REGIONS}),
            ],
        ),
        TableSpec(
            slug="order",
            columns=[
                ColumnSpec(key="number", type_id="text", is_unique=True),
                ColumnSpec(key="total", type_id="decimal", config={"precision": 12, "scale": 2}),
                ColumnSpec(key="placed", type_id="date"),
                ColumnSpec(key="customer", type_id="ref", config={"table": "customer"}),
            ],
            indexes=[IndexSpec(column_keys=("placed",)), IndexSpec(column_keys=("customer",))],
        ),
    ]
)


async def explain(s: object, run: object, *, counting: bool = False) -> str:
    sent: list[tuple[str, object]] = []
    sync_engine = s.get_bind()

    def capture(conn, cursor, statement, parameters, context, many):
        if (
            statement.lstrip().upper().startswith("SELECT")
            and "grid_row" in statement
            and ("count(" in statement) == counting
        ):
            sent.append((statement, parameters))

    event.listen(sync_engine, "before_cursor_execute", capture)
    try:
        await run
    finally:
        event.remove(sync_engine, "before_cursor_execute", capture)
    statement, parameters = sent[-1]
    conn = await s.connection()
    return "\n".join(r[0] for r in await conn.exec_driver_sql(f"EXPLAIN {statement}", parameters))


async def timed(report: Report, key: str, work: object) -> object:
    started = time.perf_counter()
    result = await work
    report.timed(key, started)
    return result


async def main() -> None:
    report = Report()
    bind = engine(pool_size=4)
    await apply_migrations(bind)
    async with session_of(bind) as s:
        ns = Namespace(s, "scale")
        await ns.apply(SCHEMA, ApplyOptions(index_builds="inline"))
        customers = await ns.table("customer")
        orders = await ns.table("order")
        made = await timed(
            report,
            "customers_write_s",
            customers.create_many([{"name": f"C{i:05d}", "region": REGIONS[i % 4]} for i in range(CUSTOMERS)]),
        )
        ids = [c.id for c in made]
        await s.commit()
        started = time.perf_counter()
        for chunk in range(0, ROWS, CHUNK):
            await orders.create_many(
                [
                    {
                        "number": f"N{i:07d}",
                        "total": f"{i % 9973}.{i % 100:02d}",
                        "placed": f"2026-{1 + i % 12:02d}-{1 + i % 28:02d}",
                        "customer": ids[i % CUSTOMERS],
                    }
                    for i in range(chunk, chunk + CHUNK)
                ]
            )
            await s.commit()
        report.timed("orders_write_s", started)
        report.set("orders_per_s", round(ROWS / report.facts["orders_write_s"]))
        await s.execute(sa.text("ANALYZE"))
        await s.commit()

        found = await timed(report, "unique_lookup_s", orders.query(Query(where=col("number") == "N0054321")))
        check([r["number"] for r in found.items] == ["N0054321"], "unique lookup")
        plan = await explain(s, orders.query(Query(where=col("number") == "N0054321")))
        check("gx_" in plan, f"unique lookup not on its index:\n{plan}")

        march = await timed(
            report, "date_range_count_s", orders.count((col("placed") >= "2026-03-01") & (col("placed") < "2026-04-01"))
        )
        check(march == sum(1 for i in range(ROWS) if 1 + i % 12 == 3), f"march count {march}")
        plan = await explain(
            s, orders.count((col("placed") >= "2026-03-01") & (col("placed") < "2026-03-03")), counting=True
        )
        check("gx_" in plan, f"date range not on its index:\n{plan}")

        one_customer = await timed(report, "ref_eq_count_s", orders.count(col("customer") == ids[7]))
        check(one_customer == ROWS // CUSTOMERS, f"orders of one customer: {one_customer}")

        north = await timed(
            report, "link_filter_count_s", orders.count(link("customer").where(col("region") == "north"))
        )
        check(north == ROWS // 4, f"north orders {north}")

        page = await timed(
            report,
            "embed_page_of_100_s",
            orders.query(
                Query(
                    order_by=(col("total").desc(),),
                    select=("number", "total", link("customer").select("name")),
                    page=OffsetPaging(size=100),
                )
            ),
        )
        check(all(r.links["customer"] is not None for r in page.items), "embed missing")

        started = time.perf_counter()
        seen, after, pages = 0, None, []
        while True:
            began = time.perf_counter()
            walked = await orders.query(
                Query(order_by=(col("placed").asc(),), select=("number",), page=CursorPaging(size=1000, after=after))
            )
            pages.append(time.perf_counter() - began)
            seen += len(walked.items)
            if not walked.has_more:
                break
            after = walked.next_cursor
        report.timed("cursor_walk_all_s", started)
        check(seen == ROWS, f"cursor walk saw {seen}")
        early, late = sorted(pages[:10])[5], sorted(pages[-10:])[5]
        report.set("cursor_page_early_ms", round(early * 1000, 1))
        report.set("cursor_page_late_ms", round(late * 1000, 1))
        check(late < 3 * early + 0.02, f"a late cursor page costs {late:.3f}s against {early:.3f}s early")

        deep = await timed(
            report,
            "offset_page_at_90000_s",
            orders.query(
                Query(order_by=(col("placed").asc(),), page=OffsetPaging(size=50, offset=min(90000, ROWS - 50)))
            ),
        )
        check(len(deep.items) == 50, "deep offset page")
    report.set("rows", ROWS)
    report.emit()
    await bind.dispose()


if __name__ == "__main__":
    asyncio.run(main())
