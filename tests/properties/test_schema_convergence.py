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

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_grid import ApplyOptions, IndexBuilds, IndexSpec, Namespace, Schema, TableSpec

INLINE = ApplyOptions(index_builds=IndexBuilds.inline)
STABLE_TYPE_OF_KEY = {
    "name": "text",
    "total": "decimal",
    "due": "date",
    "status": "enum",
    "owner": "text",
    "note": "text",
    "score": "integer",
    "flag": "boolean",
}
_KEYS = st.sampled_from(sorted(STABLE_TYPE_OF_KEY))
_SCALAR = {
    "text": {},
    "integer": {},
    "decimal": {"precision": 12, "scale": 2},
    "date": {},
    "boolean": {},
    "enum": {"options": ["a", "b"]},
}
_INDEXABLE = {"text", "integer", "decimal", "date", "enum"}


@st.composite
def tables(draw: st.DrawFn, slug: str) -> TableSpec:
    keys = draw(st.lists(_KEYS, min_size=1, max_size=5, unique=True))
    columns: list[dict[str, Any]] = []
    for order, key in enumerate(keys):
        type_id = STABLE_TYPE_OF_KEY[key]
        columns.append(
            {
                "key": key,
                "typeId": type_id,
                "config": _SCALAR[type_id],
                "isUnique": type_id in _INDEXABLE and draw(st.booleans()),
                "isRequired": draw(st.booleans()),
                "displayOrder": order,
                "label": draw(st.sampled_from(["", key.title()])),
            }
        )
    indexable = [c["key"] for c in columns if c["typeId"] in _INDEXABLE]
    indexes = []
    if indexable and draw(st.booleans()):
        indexes.append(
            IndexSpec(
                column_keys=tuple(draw(st.lists(st.sampled_from(indexable), min_size=1, max_size=2, unique=True)))
            )
        )
    return TableSpec.model_validate({"slug": slug, "columns": columns, "indexes": indexes})


schemas = st.lists(st.sampled_from(["client", "invoice", "note"]), min_size=1, max_size=3, unique=True).flatmap(
    lambda slugs: st.tuples(*(tables(s) for s in slugs)).map(lambda ts: Schema(tables=ts))
)


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(desired=schemas, then=schemas)
async def test_apply_converges_and_describe_returns_what_was_applied(
    session: AsyncSession, desired: Schema, then: Schema
) -> None:
    ns = Namespace(session, f"prop-{uuid.uuid7().hex}")
    await ns.apply(desired, INLINE)
    assert await ns.describe() == Schema(tables=tuple(sorted(desired.tables, key=lambda t: t.slug)))
    assert (await ns.apply(desired, INLINE)).plan.changes == ()

    replace = ApplyOptions(index_builds=IndexBuilds.inline, prune=True, allow_destructive=True)
    await ns.apply(then, replace)
    assert await ns.describe() == Schema(tables=tuple(sorted(then.tables, key=lambda t: t.slug)))
    assert (await ns.apply(then, replace)).plan.changes == ()
