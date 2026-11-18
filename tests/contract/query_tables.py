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
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from forktex_grid import ColumnSpec, Namespace, Row, Table, TableSpec
from tests.conftest import declare

THINGS = TableSpec(
    slug="thing",
    columns=(
        ColumnSpec(key="name", type_id="text"),
        ColumnSpec(key="count", type_id="integer"),
        ColumnSpec(key="price", type_id="decimal"),
        ColumnSpec(key="day", type_id="date"),
        ColumnSpec(key="at", type_id="datetime"),
        ColumnSpec(key="ok", type_id="boolean"),
        ColumnSpec(key="kind", type_id="enum", config={"options": ["a", "b", "c"]}),
        ColumnSpec(key="code", type_id="uuid"),
        ColumnSpec(key="tags", type_id="enum", cardinality="many", config={"options": ["x", "y"]}),
        ColumnSpec(key="data", type_id="json"),
    ),
)
CODE = uuid.uuid7()
ROWS: list[dict[str, Any]] = [
    {
        "name": "Alpha",
        "count": 1,
        "price": "1.50",
        "day": "2026-01-01",
        "at": "2026-01-01T10:00:00Z",
        "ok": True,
        "kind": "a",
        "code": str(CODE),
    },
    {
        "name": "beta",
        "count": 10,
        "price": "20",
        "day": "2026-02-01",
        "at": "2026-02-01T10:00:00Z",
        "ok": False,
        "kind": "b",
    },
    {"name": "Gamma ray", "count": 100, "price": "300.25", "day": "2026-03-01", "ok": True, "kind": "c"},
    {"name": None, "count": None},
]

CLIENT = TableSpec(
    slug="client",
    columns=(ColumnSpec(key="name", type_id="text"), ColumnSpec(key="country", type_id="text")),
)
TAG = TableSpec(slug="tag", columns=(ColumnSpec(key="name", type_id="text"),))
INVOICE = TableSpec(
    slug="invoice",
    columns=(
        ColumnSpec(key="number", type_id="text"),
        ColumnSpec(key="total", type_id="decimal"),
        ColumnSpec(key="client", type_id="ref", config={"table": "client"}),
        ColumnSpec(key="tags", type_id="ref", cardinality="many", config={"table": "tag"}),
    ),
)


async def things(session: AsyncSession) -> Table:
    (table,) = await declare(Namespace(session, "acme"), THINGS)
    await table.create_many(ROWS)
    return table


class Ledger:
    def __init__(self, clients: Table, tags: Table, invoices: Table) -> None:
        self.clients, self.tags, self.invoices = clients, tags, invoices
        self.by_name: dict[str, Row] = {}


async def ledger(session: AsyncSession) -> Ledger:
    clients, tags, invoices = await declare(Namespace(session, "acme"), CLIENT, TAG, INVOICE)
    out = Ledger(clients, tags, invoices)
    acme = await clients.create({"name": "Acme", "country": "RO"})
    globex = await clients.create({"name": "Globex", "country": "DE"})
    vip, late, new = [await tags.create({"name": n}) for n in ("vip", "late", "new")]
    for number, total, client, tagged in (
        ("INV-1", "100", acme, [vip]),
        ("INV-2", "250", acme, [vip, late]),
        ("INV-3", "75", globex, [late]),
        ("INV-4", "500", None, []),
    ):
        out.by_name[number] = await invoices.create(
            {
                "number": number,
                "total": total,
                "client": None if client is None else client.id,
                "tags": [t.id for t in tagged],
            }
        )
    out.by_name |= {"Acme": acme, "Globex": globex, "vip": vip, "late": late, "new": new}
    return out


def numbers(rows: list[Row]) -> list[str]:
    return sorted(str(r["number"]) for r in rows)
