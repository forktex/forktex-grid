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
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from forktex_grid import ColumnSpec, TableSpec
from forktex_grid.domain.fieldtypes import get_field_type
from forktex_grid.domain.row_model import row_model

STORABLE = st.characters(exclude_characters="\x00")
VALUES: dict[str, tuple[dict[str, Any], st.SearchStrategy[Any]]] = {
    "text": ({}, st.text(STORABLE, max_size=40)),
    "integer": ({}, st.integers(-(2**62), 2**62) | st.integers(-1000, 1000).map(str)),
    "decimal": (
        {"precision": 12, "scale": 4},
        st.decimals(min_value=-(10**7), max_value=10**7, places=4, allow_nan=False, allow_infinity=False),
    ),
    "boolean": ({}, st.booleans() | st.sampled_from(["yes", "no", "true", "0", 1])),
    "date": ({}, st.dates(min_value=date(1, 1, 1))),
    "datetime": ({}, st.datetimes(timezones=st.just(UTC), min_value=datetime(1, 1, 2))),
    "uuid": ({}, st.uuids()),
    "enum": ({"options": ["a", "b", "c"]}, st.sampled_from(["a", "b", "c"])),
    "json": (
        {},
        st.recursive(
            st.none() | st.booleans() | st.integers() | st.text(STORABLE, max_size=5),
            lambda c: st.lists(c, max_size=3) | st.dictionaries(st.text(STORABLE, max_size=3), c, max_size=3),
            max_leaves=6,
        ),
    ),
    "ref": ({"table": "other"}, st.uuids()),
}


@settings(max_examples=60, deadline=None)
@given(data=st.data())
def test_storing_is_idempotent_and_the_typed_model_round_trips_the_stored_form(data: st.DataObject) -> None:
    for type_id, (config, values) in VALUES.items():
        handler = get_field_type(type_id)
        parsed = handler.parse_config(config)
        value = data.draw(values, label=type_id)
        stored = handler.normalize(value, config=parsed)
        assert handler.normalize(stored, config=parsed) == stored, type_id
        model = row_model(TableSpec(slug="t", columns=(ColumnSpec(key="v", type_id=type_id, config=config),)))
        assert model.model_validate({"v": stored}).model_dump(mode="json")["v"] == stored, type_id


def test_the_typed_model_decodes_to_python_types() -> None:
    expectations = {
        "decimal": ("1.5", Decimal),
        "date": ("2026-11-13", date),
        "datetime": ("2026-11-13T10:00:00+00:00", datetime),
        "uuid": (str(uuid.uuid7()), uuid.UUID),
        "integer": (3, int),
    }
    for type_id, (stored, python) in expectations.items():
        model = row_model(TableSpec(slug="t", columns=(ColumnSpec(key="v", type_id=type_id),)))
        assert isinstance(model.model_validate({"v": stored}).v, python), type_id
