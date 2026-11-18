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

from forktex_grid import BadRequestError, Cardinality, ColumnSpec, Namespace, TableSpec, ValueCount, col, link
from tests.conftest import declare

TAG = TableSpec(slug="tag", columns=(ColumnSpec(key="name", type_id="text", is_unique=True),))
TASK = TableSpec(
    slug="task",
    columns=(
        ColumnSpec(key="status", type_id="enum", config={"options": ["todo", "doing", "done"]}),
        ColumnSpec(key="points", type_id="integer"),
        ColumnSpec(key="labels", type_id="text", cardinality=Cardinality.many),
        ColumnSpec(key="tags", type_id="ref", cardinality=Cardinality.many, config={"table": "tag"}),
        ColumnSpec(key="extra", type_id="json"),
    ),
)


async def _seed(session: AsyncSession) -> Namespace:
    ns = Namespace(session, "counts")
    tags, tasks = await declare(ns, TAG, TASK)
    red, blue = await tags.create_many([{"name": "red"}, {"name": "blue"}])
    await tasks.create_many(
        [
            {"status": "todo", "points": 1, "labels": ["a", "b"], "tags": [str(red.id)]},
            {"status": "todo", "points": 2, "labels": ["a"], "tags": [str(red.id), str(blue.id)]},
            {"status": "done", "points": 2, "labels": [], "tags": []},
            {"points": 3},
        ]
    )
    archived = await tasks.create({"status": "doing"})
    await tasks.archive(archived.id)
    return ns


async def test_counts_each_value_most_frequent_first_with_missing_values_last(session: AsyncSession) -> None:
    tasks = await (await _seed(session)).table("task")
    counted = await tasks.count_by("status")
    assert counted.groups == (
        ValueCount(value="todo", count=2),
        ValueCount(value="done", count=1),
        ValueCount(value=None, count=1),
    )
    assert counted.others == 0


async def test_values_come_back_in_their_stored_form_and_ties_by_value(session: AsyncSession) -> None:
    tasks = await (await _seed(session)).table("task")
    assert [(g.value, g.count) for g in (await tasks.count_by("points")).groups] == [(2, 2), (1, 1), (3, 1)]


async def test_a_list_column_counts_each_element(session: AsyncSession) -> None:
    ns = await _seed(session)
    tasks = await ns.table("task")
    assert [(g.value, g.count) for g in (await tasks.count_by("labels")).groups] == [("a", 2), ("b", 1)]
    by_tag = (await tasks.count_by("tags")).groups
    assert [g.count for g in by_tag] == [2, 1]


async def test_a_filter_narrows_what_is_counted_through_links_too(session: AsyncSession) -> None:
    tasks = await (await _seed(session)).table("task")
    assert (await tasks.count_by("status", col("points") >= 2)).groups == (
        ValueCount(value="done", count=1),
        ValueCount(value="todo", count=1),
        ValueCount(value=None, count=1),
    )
    blue = link("tags").where(col("name") == "blue")
    assert (await tasks.count_by("status", blue)).groups == (ValueCount(value="todo", count=1),)


async def test_what_falls_past_the_limit_is_counted_as_others(session: AsyncSession) -> None:
    tasks = await (await _seed(session)).table("task")
    counted = await tasks.count_by("status", limit=1)
    assert counted.groups == (ValueCount(value="todo", count=2),)
    assert counted.others == 2
    assert sum(g.count for g in counted.groups) + counted.others == await tasks.count()


async def test_refuses_what_cannot_be_grouped(session: AsyncSession) -> None:
    tasks = await (await _seed(session)).table("task")
    with pytest.raises(BadRequestError, match="no column 'nope'"):
        await tasks.count_by("nope")
    with pytest.raises(BadRequestError, match="limit must be between 1 and 1000"):
        await tasks.count_by("status", limit=0)
    with pytest.raises(BadRequestError, match=r"'extra' \(json\) has no equality"):
        await tasks.count_by("extra")


async def test_row_counts_cover_every_live_table_of_the_namespace_only(session: AsyncSession) -> None:
    ns = await _seed(session)
    await declare(Namespace(session, "elsewhere"), TAG)
    assert await ns.row_counts() == {"tag": 2, "task": 4}
    assert await Namespace(session, "nobody").row_counts() == {}


async def test_a_cleared_list_counts_no_elements(session: AsyncSession) -> None:
    ns = await _seed(session)
    tasks = await ns.table("task")
    first = (await tasks.query()).items[0]
    await tasks.patch(first.id, values={"labels": None})
    counted = await tasks.count_by("labels")
    assert sum(g.count for g in counted.groups) + counted.others >= 0
    assert all(g.value is not None for g in counted.groups)
