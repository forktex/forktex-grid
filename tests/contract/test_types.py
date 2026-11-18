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

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import pytest
from forktex.types import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_grid import (
    AlreadyExistsError,
    BadRequestError,
    Capabilities,
    ColumnSpec,
    FieldType,
    FieldTypeHandler,
    FilterOp,
    Namespace,
    NoConfig,
    Query,
    TableSpec,
    col,
    is_registered,
    register_field_type,
)
from forktex_grid.domain.fieldtypes import get_field_type
from tests.conftest import declare


def _normalize(type_id: str, value: object, config: dict[str, Any] | None = None) -> JsonValue:
    handler = get_field_type(type_id)
    return handler.normalize(value, config=handler.parse_config(config or {}))


def test_every_builtin_is_registered() -> None:
    assert all(is_registered(t) for t in FieldType)


@pytest.mark.parametrize(
    ("type_id", "config", "value", "stored"),
    [
        ("text", {}, "hello", "hello"),
        ("text", {}, 42, "42"),
        ("text", {"subtype": "email"}, "a@b.ro", "a@b.ro"),
        ("integer", {}, 7, 7),
        ("integer", {}, "  12 ", 12),
        ("integer", {}, 3.0, 3),
        ("decimal", {}, "250.00", "250.00"),
        ("decimal", {}, 1.5, "1.5"),
        ("decimal", {"precision": 4, "scale": 2}, Decimal("12.34"), "12.34"),
        ("boolean", {}, True, True),
        ("boolean", {}, "yes", True),
        ("boolean", {}, 0, False),
        ("date", {}, "2026-03-01", "2026-03-01"),
        ("date", {}, dt.date(2026, 3, 1), "2026-03-01"),
        ("date", {}, dt.datetime(2026, 3, 1, 23, 0, tzinfo=dt.UTC), "2026-03-01"),
        ("datetime", {}, dt.datetime(2026, 3, 1, 12, 0, tzinfo=dt.UTC), "2026-03-01T12:00:00+00:00"),
        ("uuid", {}, uuid.UUID(int=1), str(uuid.UUID(int=1))),
        ("enum", {"options": ["a", "b"]}, "a", "a"),
        ("json", {}, {"a": [1, (2, 3)]}, {"a": [1, [2, 3]]}),
        ("ref", {"table": "client"}, str(uuid.UUID(int=5)), str(uuid.UUID(int=5))),
    ],
)
def test_a_builtin_stores_its_canonical_form(type_id: str, config: dict, value: object, stored: JsonValue) -> None:
    assert _normalize(type_id, value, config) == stored


@pytest.mark.parametrize("type_id", list(FieldType))
def test_none_is_stored_as_none(type_id: FieldType) -> None:
    config = {"enum": {"options": ["a"]}, "ref": {"table": "t"}}.get(type_id, {})
    assert _normalize(type_id, None, config) is None


def test_datetimes_are_stored_in_utc() -> None:
    plus_two = dt.datetime(2026, 3, 1, 14, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))
    assert _normalize("datetime", plus_two) == _normalize("datetime", "2026-03-01T12:00:00+00:00")


@pytest.mark.parametrize(
    ("type_id", "config", "value"),
    [
        ("text", {}, True),
        ("text", {}, ["a"]),
        ("text", {}, "nul\x00"),
        ("text", {"max_length": 3}, "four"),
        ("text", {"subtype": "email"}, "nobody"),
        ("integer", {}, True),
        ("integer", {}, 1.5),
        ("integer", {}, "x"),
        ("integer", {}, float("nan")),
        ("decimal", {}, "abc"),
        ("decimal", {}, "NaN"),
        ("decimal", {}, False),
        ("decimal", {"precision": 4, "scale": 2}, "1.234"),
        ("decimal", {"precision": 4, "scale": 2}, "123.4"),
        ("boolean", {}, "maybe"),
        ("boolean", {}, 2),
        ("date", {}, "01/03/2026"),
        ("date", {}, 20260301),
        ("datetime", {}, "yesterday"),
        ("uuid", {}, "not-a-uuid"),
        ("enum", {"options": ["a"]}, "b"),
        ("json", {}, {"x": float("inf")}),
        ("json", {}, object()),
        ("ref", {"table": "t"}, "row-1"),
    ],
)
def test_a_builtin_refuses_what_it_cannot_hold(type_id: str, config: dict, value: object) -> None:
    with pytest.raises(ValueError):
        _normalize(type_id, value, config)


def test_capabilities_decide_filters_sorts_and_indexes() -> None:
    text = get_field_type("text").capabilities
    assert FilterOp.fuzzy in text.filter_ops and text.sortable and text.index_kinds == {"btree", "trgm"}
    json = get_field_type("json").capabilities
    assert not json.filter_ops and not json.sortable and not json.index_kinds
    assert not get_field_type("uuid").capabilities.sortable


def _custom() -> FieldTypeHandler:

    class Code(FieldTypeHandler):
        type_id = f"code_{uuid.uuid7().hex}"
        config_model = NoConfig
        capabilities = Capabilities(
            filter_ops=frozenset({FilterOp.eq}), sortable=True, index_kinds=frozenset({"btree"})
        )

        def normalize(self, value: object, *, config: NoConfig) -> JsonValue:
            if value is None:
                return None
            if not isinstance(value, str) or not value.isalnum():
                raise ValueError(f"{value!r} is not a code")
            return value.upper()

    return Code()


def test_a_type_registers_once_unless_replaced() -> None:
    handler = _custom()
    assert not is_registered(handler.type_id)
    register_field_type(handler)
    assert is_registered(handler.type_id)
    with pytest.raises(AlreadyExistsError, match="already registered"):
        register_field_type(handler)
    register_field_type(handler, replace=True)
    assert get_field_type(handler.type_id) is handler


async def test_a_custom_type_is_checked_stored_and_filtered(session: AsyncSession) -> None:
    handler = _custom()
    register_field_type(handler)
    ns = Namespace(session, "types")
    (devices,) = await declare(ns, TableSpec(slug="device", columns=[ColumnSpec(key="code", type_id=handler.type_id)]))
    await devices.create({"code": "ab1"})
    await devices.create({"code": "zz9"})
    with pytest.raises(BadRequestError, match="column 'code'"):
        await devices.create({"code": "a b"})
    page = await devices.query(Query(where=col("code") == "ab1"))
    assert [r["code"] for r in page.items] == ["AB1"]
    with pytest.raises(BadRequestError, match="does not take"):
        await devices.query(Query(where=col("code") != "ab1"))
