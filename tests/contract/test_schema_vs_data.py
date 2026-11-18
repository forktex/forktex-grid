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

from forktex_grid import ApplyOptions, BadRequestError, ColumnSpec, IndexBuilds, Namespace, Schema, TableSpec
from tests.conftest import declare

ALLOW = ApplyOptions(index_builds=IndexBuilds.inline, allow_destructive=True)
NAME = ColumnSpec(key="name", type_id="text")


def table(*columns: ColumnSpec) -> Schema:
    return Schema(tables=[TableSpec(slug="item", columns=(NAME, *columns))])


async def _items(session: AsyncSession, *rows: dict) -> Namespace:
    ns = Namespace(session, "acme")
    (items,) = await declare(
        ns,
        TableSpec(slug="item", columns=(NAME, ColumnSpec(key="kind", type_id="enum", config={"options": ["a", "b"]}))),
    )
    await items.create_many(list(rows))
    return ns


async def test_a_new_column_with_a_default_fills_the_rows_that_exist(session: AsyncSession) -> None:
    ns = await _items(session, {"name": "x"}, {"name": "y"})
    stage = ColumnSpec(
        key="stage", type_id="enum", config={"options": ["new", "done"]}, default_value="new", is_required=True
    )
    await ns.apply(table(ColumnSpec(key="kind", type_id="enum", config={"options": ["a", "b"]}), stage), ALLOW)
    items = await ns.table("item")
    assert {r["stage"] for r in (await items.query()).items} == {"new"}
    assert all(items.typed(r) for r in (await items.query()).items)


async def test_a_required_column_without_a_default_is_refused_while_rows_exist(session: AsyncSession) -> None:
    ns = await _items(session, {"name": "x"})
    with pytest.raises(BadRequestError, match="1 existing row"):
        await ns.apply(table(ColumnSpec(key="owner", type_id="text", is_required=True)), ALLOW)
    assert [c.key for c in (await ns.describe()).table("item").columns] == ["name", "kind"]


async def test_a_required_column_without_a_default_is_fine_on_an_empty_table(session: AsyncSession) -> None:
    ns = await _items(session)
    await ns.apply(table(ColumnSpec(key="owner", type_id="text", is_required=True)), ALLOW)


@pytest.mark.parametrize(
    "tightened",
    [
        ColumnSpec(key="kind", type_id="enum", config={"options": ["a"]}),
        ColumnSpec(key="kind", type_id="enum", config={"options": ["a", "b"]}, is_required=True),
    ],
)
async def test_tightening_a_column_is_refused_while_a_row_would_break_it(
    session: AsyncSession, tightened: ColumnSpec
) -> None:
    ns = await _items(session, {"name": "x", "kind": "b"}, {"name": "y"})
    with pytest.raises(BadRequestError, match="do not fit"):
        await ns.apply(table(tightened), ALLOW)
    assert (await ns.describe()).table("item").column("kind").config == {"options": ["a", "b"]}


async def test_tightening_succeeds_once_the_rows_fit(session: AsyncSession) -> None:
    ns = await _items(session, {"name": "x", "kind": "a"})
    await ns.apply(table(ColumnSpec(key="kind", type_id="enum", config={"options": ["a"]}, is_required=True)), ALLOW)
    assert (await ns.describe()).table("item").column("kind").is_required
