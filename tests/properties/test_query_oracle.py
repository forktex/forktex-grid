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

import random
import uuid
from decimal import Decimal
from typing import Any

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_grid import ColumnSpec, Namespace, OffsetPaging, Query, Table, TableSpec
from tests.conftest import declare

KINDS = ["a", "b", "c"]
SAMPLE = TableSpec(
    slug="sample",
    columns=(
        ColumnSpec(key="n", type_id="integer"),
        ColumnSpec(key="price", type_id="decimal"),
        ColumnSpec(key="ok", type_id="boolean"),
        ColumnSpec(key="kind", type_id="enum", config={"options": KINDS}),
        ColumnSpec(key="name", type_id="text"),
    ),
)
NAMES = ["alpha", "alps", "beta", "Bet", "gamma", ""]


def dataset(seed: int, size: int = 60) -> list[dict[str, Any]]:
    rng = random.Random(seed)

    def maybe(value: Any) -> Any:
        return None if rng.random() < 0.15 else value

    return [
        {
            "n": maybe(rng.randint(-5, 5)),
            "price": maybe(str(Decimal(rng.randint(-500, 500)) / 100)),
            "ok": maybe(rng.random() < 0.5),
            "kind": maybe(rng.choice(KINDS)),
            "name": maybe(rng.choice(NAMES)),
        }
        for _ in range(size)
    ]


ROWS = dataset(seed=20261113)

_ints = st.integers(-6, 6)
_prices = st.integers(-600, 600).map(lambda c: str(Decimal(c) / 100))
_ORDERED = {"n": _ints, "price": _prices}

comparisons = st.one_of(
    st.builds(
        lambda col, op, v: {"column": col, "op": op, "value": v},
        st.sampled_from(["n", "price"]),
        st.sampled_from(["eq", "ne", "lt", "lte", "gt", "gte"]),
        st.just(None),
    ).flatmap(lambda c: _ORDERED[c["column"]].map(lambda v: {**c, "value": v})),
    st.builds(lambda v: {"column": "ok", "op": "eq", "value": v}, st.booleans()),
    st.builds(lambda vs: {"column": "kind", "op": "in", "value": vs}, st.lists(st.sampled_from(KINDS), max_size=3)),
    st.builds(lambda v: {"column": "kind", "op": "ne", "value": v}, st.sampled_from(KINDS)),
    st.builds(
        lambda op, v: {"column": "name", "op": op, "value": v},
        st.sampled_from(["eq", "startswith", "contains"]),
        st.sampled_from(["al", "alpha", "Be", "e", "a"]),
    ),
    st.builds(
        lambda col, v: {"column": col, "op": "is_null", "value": v},
        st.sampled_from(["n", "price", "ok", "kind", "name"]),
        st.booleans(),
    ),
)

wheres = st.recursive(
    comparisons,
    lambda inner: st.one_of(
        st.lists(inner, min_size=1, max_size=3).map(lambda xs: {"and": xs}),
        st.lists(inner, min_size=1, max_size=3).map(lambda xs: {"or": xs}),
        inner.map(lambda x: {"not": x}),
    ),
    max_leaves=6,
)


def _cmp(op: str, a: Any, b: Any) -> bool | None:
    if a is None or b is None:
        return None
    return {
        "eq": a == b,
        "ne": a != b,
        "lt": a < b,
        "lte": a <= b,
        "gt": a > b,
        "gte": a >= b,
    }[op]


def three_valued_truth(where: dict[str, Any], row: dict[str, Any]) -> bool | None:
    if "and" in where:
        parts = [three_valued_truth(w, row) for w in where["and"]]
        return False if False in parts else None if None in parts else True
    if "or" in where:
        parts = [three_valued_truth(w, row) for w in where["or"]]
        return True if True in parts else None if None in parts else False
    if "not" in where:
        inner = three_valued_truth(where["not"], row)
        return None if inner is None else not inner
    column, op, value = where["column"], where["op"], where["value"]
    stored = row.get(column)
    if op == "is_null":
        return (stored is None) == value
    if column == "price":
        stored, value = (None if stored is None else Decimal(stored)), Decimal(value)
    if op == "in":
        if not value:
            return False
        return None if stored is None else stored in value
    if op == "startswith":
        return None if stored is None else stored.startswith(value)
    if op == "contains":
        return None if stored is None else value in stored
    return _cmp(op, stored, value)


async def _sample(session: AsyncSession) -> Table:
    ns = Namespace(session, "oracle")
    if not (await ns.describe()).tables:
        (table,) = await declare(ns, SAMPLE)
        await table.create_many(ROWS)
    return await ns.table("sample")


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@example(where={"not": {"column": "kind", "op": "in", "value": []}})
@given(where=wheres)
async def test_a_query_returns_exactly_the_rows_its_filter_holds_for(session: AsyncSession, where: dict) -> None:
    table = await _sample(session)
    page = await table.query(Query.model_validate({"where": where, "page": {"size": 1000}}))
    by_id = {r.id: r for r in (await table.query(Query(page=OffsetPaging(size=1000)))).items}
    expected = {i for i, r in by_id.items() if three_valued_truth(where, dict(r.values)) is True}
    assert {r.id for r in page.items} == expected
    assert await table.count(Query.model_validate({"where": where}).where) == len(expected)


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(where=wheres, column=st.sampled_from(["n", "price", "ok", "kind", "name"]), limit=st.integers(1, 8))
async def test_counts_by_value_match_grouping_the_rows_the_filter_holds_for(
    session: AsyncSession, where: dict, column: str, limit: int
) -> None:
    table = await _sample(session)
    rows = (await table.query(Query(page=OffsetPaging(size=1000)))).items
    expected: dict[Any, int] = {}
    for r in rows:
        if three_valued_truth(where, dict(r.values)) is True:
            expected[r.get(column)] = expected.get(r.get(column), 0) + 1
    counted = await table.count_by(column, Query.model_validate({"where": where}).where, limit=limit)
    found = [(g.value, g.count) for g in counted.groups]
    assert len(found) == min(limit, len(expected))
    assert all(expected[value] == count for value, count in found)
    assert [c for _, c in found] == sorted((c for _, c in found), reverse=True)
    left_out = sorted(expected.values(), reverse=True)[len(found) :]
    assert counted.others == sum(left_out)


@settings(max_examples=200, deadline=None)
@given(where=wheres)
def test_every_where_document_round_trips(where: dict) -> None:
    query = Query.model_validate({"where": where})
    assert Query.model_validate(query.model_dump(mode="json")) == query
    assert Query.model_validate_json(query.model_dump_json()) == query


def test_the_oracle_sees_a_varied_dataset() -> None:
    assert len({r["kind"] for r in ROWS}) == 4 and any(r["n"] is None for r in ROWS)
    assert uuid.uuid7()
