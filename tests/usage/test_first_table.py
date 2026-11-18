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

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from forktex_grid import ColumnSpec, Namespace, Query, Schema, TableSpec, col

INVOICES = Schema(
    tables=[
        TableSpec(
            slug="invoice",
            columns=[
                ColumnSpec(key="number", type_id="text", is_required=True, is_unique=True),
                ColumnSpec(key="total", type_id="decimal"),
                ColumnSpec(key="status", type_id="enum", config={"options": ["draft", "sent", "paid"]}),
            ],
        )
    ]
)


async def test_declare_a_table_write_rows_and_ask_for_them(session: AsyncSession) -> None:
    acme = Namespace(session, "acme")
    await acme.apply(INVOICES)
    invoices = await acme.table("invoice")

    await invoices.create_many(
        [
            {"number": "INV-001", "total": "250.00", "status": "sent"},
            {"number": "INV-002", "total": "80.00", "status": "paid"},
            {"number": "INV-003", "total": "1200.00", "status": "sent"},
        ]
    )

    unpaid = await invoices.query(
        Query(where=(col("status") == "sent") & (col("total") >= 100), order_by=(col("total").desc(),), count=True)
    )
    assert [r["number"] for r in unpaid.items] == ["INV-003", "INV-001"] and unpaid.total == 2

    largest = invoices.typed(unpaid.items[0])
    assert largest.total == Decimal("1200.00")


async def test_the_same_question_arrives_as_json(session: AsyncSession) -> None:
    acme = Namespace(session, "acme")
    await acme.apply(Schema.model_validate(INVOICES.model_dump(mode="json")))
    invoices = await acme.table("invoice")
    await invoices.create({"number": "INV-001", "total": "250.00", "status": "sent"})

    question = {
        "where": {
            "and": [{"column": "status", "op": "eq", "value": "sent"}, {"column": "total", "op": "gte", "value": 100}]
        },
        "orderBy": [{"column": "total", "direction": "desc"}],
        "select": ["number"],
    }
    page = await invoices.query(Query.model_validate(question))
    assert page.model_dump(mode="json")["items"][0]["values"] == {"number": "INV-001"}


async def test_the_readme_example(session: AsyncSession) -> None:
    acme = Namespace(session, "acme")
    await acme.apply(
        Schema(
            tables=[
                TableSpec(
                    slug="invoice",
                    columns=[
                        ColumnSpec(key="number", type_id="text", is_unique=True),
                        ColumnSpec(key="total", type_id="decimal"),
                    ],
                )
            ]
        )
    )

    invoices = await acme.table("invoice")
    await invoices.create({"number": "INV-001", "total": "250.00"})
    page = await invoices.query(Query(where=col("total") >= 100))

    assert [r["number"] for r in page.items] == ["INV-001"]
