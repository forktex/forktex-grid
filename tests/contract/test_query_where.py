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

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_grid import BadRequestError, ColumnSpec, Query, Table, col
from tests.contract.query_tables import CODE, things


async def _names(table: Table, where: dict[str, Any]) -> list[Any]:
    page = await table.query(Query.model_validate({"where": where}))
    return sorted((r.get("name") or "") for r in page.items)


@pytest.mark.parametrize(
    ("where", "names"),
    [
        ({"column": "name", "op": "eq", "value": "beta"}, ["beta"]),
        ({"column": "name", "op": "contains", "value": "amm"}, ["Gamma ray"]),
        ({"column": "name", "op": "icontains", "value": "ALPHA"}, ["Alpha"]),
        ({"column": "name", "op": "startswith", "value": "Ga"}, ["Gamma ray"]),
        ({"column": "count", "op": "gte", "value": 10}, ["Gamma ray", "beta"]),
        ({"column": "count", "op": "between", "value": [5, 50]}, ["beta"]),
        ({"column": "price", "op": "lt", "value": "20"}, ["Alpha"]),
        ({"column": "price", "op": "between", "value": ["2", "300.25"]}, ["Gamma ray", "beta"]),
        ({"column": "day", "op": "gt", "value": "2026-01-15"}, ["Gamma ray", "beta"]),
        ({"column": "at", "op": "lte", "value": "2026-01-31T00:00:00Z"}, ["Alpha"]),
        ({"column": "ok", "op": "eq", "value": True}, ["Alpha", "Gamma ray"]),
        ({"column": "kind", "op": "in", "value": ["a", "c"]}, ["Alpha", "Gamma ray"]),
        ({"column": "code", "op": "eq", "value": str(CODE)}, ["Alpha"]),
        ({"column": "count", "op": "is_null", "value": True}, [""]),
        (
            {"and": [{"column": "ok", "op": "eq", "value": True}, {"column": "count", "op": "gt", "value": 5}]},
            ["Gamma ray"],
        ),
        (
            {"or": [{"column": "kind", "op": "eq", "value": "a"}, {"column": "kind", "op": "eq", "value": "b"}]},
            ["Alpha", "beta"],
        ),
        ({"not": {"column": "ok", "op": "eq", "value": True}}, ["beta"]),
    ],
)
async def test_a_filter_selects_by_typed_value(session: AsyncSession, where: dict[str, Any], names: list[str]) -> None:
    assert await _names(await things(session), where) == names


def test_the_builder_writes_the_same_document_as_json() -> None:
    built = Query(where=(col("name") == "beta") & (col("count") > 5) | ~col("kind").is_in(["a"]))
    written = Query.model_validate(
        {
            "where": {
                "or": [
                    {
                        "and": [
                            {"column": "name", "op": "eq", "value": "beta"},
                            {"column": "count", "op": "gt", "value": 5},
                        ]
                    },
                    {"not": {"column": "kind", "op": "in", "value": ["a"]}},
                ]
            }
        }
    )
    assert built == written
    assert Query.model_validate(built.model_dump(mode="json")) == built


def test_chained_ands_and_ors_stay_one_flat_list() -> None:
    a, b, c = col("x") == 1, col("y") == 2, col("z") == 3
    assert (a & b & c).model_dump(mode="json") == {"and": [n.model_dump(mode="json") for n in (a, b, c)]}
    assert (a | b | c).model_dump(mode="json") == {"or": [n.model_dump(mode="json") for n in (a, b, c)]}


async def test_the_fuzzy_filter_matches_similar_text(session: AsyncSession) -> None:
    assert "Alpha" in await _names(await things(session), {"column": "name", "op": "fuzzy", "value": "Alphaa"})


@pytest.mark.parametrize(
    "where",
    [
        {"column": "ok", "op": "gt", "value": True},
        {"column": "code", "op": "contains", "value": "a"},
        {"column": "tags", "op": "eq", "value": "x"},
        {"column": "data", "op": "eq", "value": 1},
        {"column": "missing", "op": "eq", "value": 1},
        {"column": "count", "op": "eq", "value": "many"},
    ],
)
async def test_a_filter_a_column_does_not_take_is_refused(session: AsyncSession, where: dict[str, Any]) -> None:
    table = await things(session)
    with pytest.raises(BadRequestError):
        await table.query(Query.model_validate({"where": where}))


async def test_system_fields_filter_and_sort_beside_columns(session: AsyncSession) -> None:
    table = await things(session)
    rows = (await table.query(Query(order_by=(col("created_at").asc(),)))).items
    first = rows[0]
    assert [r.id for r in (await table.query(Query(where=col("id") == first.id))).items] == [first.id]
    later = datetime.now(UTC) + timedelta(days=1)
    assert (await table.query(Query(where=col("created_at") >= later))).items == []
    assert await table.count(col("updated_at") < later) == len(rows)
    await table.create({"name": "keyed"}, external_ref="K-1")
    keyed = await table.query(Query(where=col("external_ref").startswith("K-")))
    assert [r["name"] for r in keyed.items] == ["keyed"]


async def test_a_system_field_takes_only_what_its_type_takes(session: AsyncSession) -> None:
    table = await things(session)
    with pytest.raises(BadRequestError):
        await table.query(Query(where=col("id").contains("1")))
    with pytest.raises(BadRequestError):
        await table.query(Query(where=col("created_at") == "yesterday"))


def test_a_column_may_not_take_a_system_key() -> None:
    for key in ("id", "external_ref", "created_at", "updated_at"):
        with pytest.raises(BadRequestError, match="reserved"):
            ColumnSpec(key=key, type_id="text")
