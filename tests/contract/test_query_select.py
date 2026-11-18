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

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_grid import BadRequestError, Namespace, Query, Row, col, link
from tests.conftest import declare
from tests.contract.query_tables import THINGS, ledger


async def test_select_returns_only_the_named_columns(session: AsyncSession) -> None:
    books = await ledger(session)
    page = await books.invoices.query(Query(select=("number",), where=col("number") == "INV-1"))
    (row,) = page.items
    assert row.values == {"number": "INV-1"} and row.id == books.by_name["INV-1"].id


async def test_an_embed_returns_the_linked_row_with_its_own_select(session: AsyncSession) -> None:
    books = await ledger(session)
    query = Query(select=("number", "client", link("client").select("name")), where=col("number") == "INV-1")
    (row,) = (await books.invoices.query(query)).items
    assert row.values == {"number": "INV-1", "client": str(books.by_name["Acme"].id)}
    embedded = row.links["client"]
    assert isinstance(embedded, Row) and embedded.values == {"name": "Acme"}


async def test_an_embed_of_a_many_link_returns_every_linked_row(session: AsyncSession) -> None:
    books = await ledger(session)
    query = Query.model_validate(
        {
            "select": ["number", {"link": "tags", "select": ["name"]}],
            "where": {"column": "number", "op": "eq", "value": "INV-2"},
        }
    )
    (row,) = (await books.invoices.query(query)).items
    tags = row.links["tags"]
    assert isinstance(tags, tuple) and sorted(str(t["name"]) for t in tags) == ["late", "vip"]
    assert "tags" not in row.values


async def test_a_missing_link_embeds_as_none_or_empty(session: AsyncSession) -> None:
    books = await ledger(session)
    query = Query(select=(link("client").select("name"), link("tags").select()), where=col("number") == "INV-4")
    (row,) = (await books.invoices.query(query)).items
    assert row.links == {"client": None, "tags": ()} and row.values == {}


async def test_rows_carry_no_links_unless_asked(session: AsyncSession) -> None:
    books = await ledger(session)
    assert all(r.links == {} for r in (await books.invoices.query()).items)


@pytest.mark.parametrize(
    "select",
    [("nope",), (link("number").select("name"),), (link("client").select("nope"),)],
)
async def test_a_select_naming_what_does_not_exist_is_refused(session: AsyncSession, select: tuple) -> None:
    books = await ledger(session)
    with pytest.raises(BadRequestError):
        await books.invoices.query(Query(select=select))


async def test_a_system_field_in_select_is_accepted_and_always_on_the_row(session: AsyncSession) -> None:
    (table,) = await declare(Namespace(session, "acme"), THINGS)
    created = await table.create({"count": 3}, external_ref="t-1")
    (row,) = (await table.query(Query(select=("id", "external_ref", "created_at")))).items
    assert row.id == created.id and row.external_ref == "t-1" and row.values == {}
