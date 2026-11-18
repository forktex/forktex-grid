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

from forktex_grid import BadRequestError, Query, col, link
from tests.contract.query_tables import ledger, numbers


async def test_a_link_filters_by_a_column_of_the_linked_row(session: AsyncSession) -> None:
    books = await ledger(session)
    page = await books.invoices.query(Query(where=link("client").where(col("country") == "RO")))
    assert numbers(page.items) == ["INV-1", "INV-2"]


async def test_the_json_link_node_is_the_same_query(session: AsyncSession) -> None:
    books = await ledger(session)
    document = {"where": {"link": "client", "where": {"column": "country", "op": "eq", "value": "RO"}}}
    assert Query.model_validate(document) == Query(where=link("client").where(col("country") == "RO"))
    assert numbers((await books.invoices.query(Query.model_validate(document))).items) == ["INV-1", "INV-2"]


@pytest.mark.parametrize(
    ("match", "expected"),
    [
        ("some", ["INV-2", "INV-3"]),
        ("none", ["INV-1", "INV-4"]),
        ("every", ["INV-3", "INV-4"]),
    ],
)
async def test_a_many_link_matches_some_every_or_none(session: AsyncSession, match: str, expected: list[str]) -> None:
    books = await ledger(session)
    condition = getattr(link("tags"), match)(col("name") == "late")
    assert numbers((await books.invoices.query(Query(where=condition))).items) == expected


async def test_a_one_link_with_no_target_matches_none_and_every(session: AsyncSession) -> None:
    books = await ledger(session)
    assert numbers((await books.invoices.query(Query(where=link("client").none(col("country") == "RO")))).items) == [
        "INV-3",
        "INV-4",
    ]


async def test_a_link_combines_with_the_table_own_columns(session: AsyncSession) -> None:
    books = await ledger(session)
    where = (col("total") >= 200) & link("client").where(col("name") == "Acme")
    assert numbers((await books.invoices.query(Query(where=where))).items) == ["INV-2"]
    assert await books.invoices.count(where) == 1


async def test_a_link_follows_one_level_only(session: AsyncSession) -> None:
    books = await ledger(session)
    deeper = link("client").where(link("client").where(col("name") == "x"))
    with pytest.raises(BadRequestError, match="level"):
        await books.invoices.query(Query(where=deeper))


async def test_a_link_must_name_a_ref_column(session: AsyncSession) -> None:
    books = await ledger(session)
    with pytest.raises(BadRequestError, match="not a link"):
        await books.invoices.query(Query(where=link("number").where(col("name") == "x")))


async def test_an_archived_target_no_longer_matches(session: AsyncSession) -> None:
    books = await ledger(session)
    await books.invoices.archive(books.by_name["INV-3"].id)
    await books.clients.archive(books.by_name["Globex"].id)
    assert await books.invoices.count(link("client").where(col("country") == "DE")) == 0
