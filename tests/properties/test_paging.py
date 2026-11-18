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

from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_grid import CursorPaging, OffsetPaging, Query, SortKey, Table
from tests.properties.test_query_oracle import _sample

sorts = st.lists(
    st.sampled_from(["n", "price", "ok", "kind", "name", "created_at"]), min_size=1, max_size=3, unique=True
).flatmap(lambda cols: st.sampled_from(["asc", "desc"]).map(lambda d: [{"column": c, "direction": d} for c in cols]))
sizes = st.integers(3, 17)
FUNCTION_FIXTURE = [HealthCheck.function_scoped_fixture]


async def _all(table: Table, order: list[dict[str, Any]]) -> list[Any]:
    keys = tuple(SortKey.model_validate(k) for k in order)
    return [r.id for r in (await table.query(Query(order_by=keys, page=OffsetPaging(size=1000)))).items]


@settings(max_examples=25, deadline=None, suppress_health_check=FUNCTION_FIXTURE)
@given(order=sorts, size=sizes)
async def test_cursor_pages_visit_every_row_once_in_the_full_order(
    session: AsyncSession, order: list[dict[str, Any]], size: int
) -> None:
    table = await _sample(session)
    keys = tuple(SortKey.model_validate(k) for k in order)
    walked: list[Any] = []
    after = None
    while True:
        page = await table.query(Query(order_by=keys, page=CursorPaging(size=size, after=after)))
        walked += [r.id for r in page.items]
        if not page.has_more:
            break
        after = page.next_cursor
    assert walked == await _all(table, order)


@settings(max_examples=25, deadline=None, suppress_health_check=FUNCTION_FIXTURE)
@given(order=sorts, size=sizes)
async def test_offset_pages_tile_the_full_order(session: AsyncSession, order: list[dict[str, Any]], size: int) -> None:
    table = await _sample(session)
    keys = tuple(SortKey.model_validate(k) for k in order)
    walked: list[Any] = []
    offset = 0
    while True:
        page = await table.query(Query(order_by=keys, page=OffsetPaging(size=size, offset=offset), count=True))
        walked += [r.id for r in page.items]
        assert page.total == 60
        if not page.has_more:
            break
        offset += size
    assert walked == await _all(table, order)
