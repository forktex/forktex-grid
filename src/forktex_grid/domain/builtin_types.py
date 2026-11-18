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

import json
import math
import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from forktex.database.identifiers import validate_slug
from forktex.iso import from_date_iso, from_iso, to_date_iso, to_iso
from forktex.types import BaseWireValueObject, JsonValue
from pydantic import ConfigDict, Field, StringConstraints, model_validator
from typing_extensions import TypeForm

from forktex_grid.domain.enums import FieldType, OnDelete
from forktex_grid.domain.fieldtypes import (
    Capabilities,
    FieldTypeHandler,
    FilterOp,
    IndexKind,
    NoConfig,
    annotated,
    is_registered,
    one_of,
    register_field_type,
)
from forktex_grid.domain.limits import MAX_INTEGER, MIN_INTEGER, holds_nul

_EQUALITY = frozenset({FilterOp.eq, FilterOp.ne, FilterOp.in_, FilterOp.not_in, FilterOp.is_null})
_ORDERED = _EQUALITY | {FilterOp.lt, FilterOp.lte, FilterOp.gt, FilterOp.gte, FilterOp.between}
_TEXT = _EQUALITY | {
    FilterOp.contains,
    FilterOp.icontains,
    FilterOp.starts_with,
    FilterOp.ends_with,
    FilterOp.fuzzy,
}
_BTREE: frozenset[IndexKind] = frozenset({"btree"})


class TextConfig(BaseWireValueObject):
    model_config = ConfigDict(extra="forbid")

    max_length: int | None = Field(default=None, ge=1)
    subtype: str | None = None


class TextType(FieldTypeHandler[TextConfig]):
    type_id = FieldType.text
    config_model = TextConfig
    capabilities = Capabilities(filter_ops=_TEXT, sortable=True, index_kinds=frozenset({"btree", "trgm"}))

    def normalize(self, value: object, *, config: TextConfig) -> JsonValue:
        if value is None:
            return None
        if not isinstance(value, str | int | float | Decimal) or isinstance(value, bool):
            raise ValueError(f"{value!r} is not text")
        text = str(value)
        if "\x00" in text:
            raise ValueError("text may not contain a NUL character")
        if config.max_length is not None and len(text) > config.max_length:
            raise ValueError(f"longer than {config.max_length} characters")
        if config.subtype == "email" and "@" not in text:
            raise ValueError(f"{text!r} is not an e-mail address")
        return text

    def python_type(self, config: TextConfig) -> TypeForm[Any]:
        return str if config.max_length is None else annotated(str, StringConstraints(max_length=config.max_length))


class IntegerType(FieldTypeHandler):
    type_id = FieldType.integer
    config_model = NoConfig
    pg_cast = "bigint"
    capabilities = Capabilities(filter_ops=_ORDERED, sortable=True, index_kinds=_BTREE)

    def normalize(self, value: object, *, config: NoConfig) -> JsonValue:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError("a boolean is not an integer")
        if isinstance(value, int):
            whole = value
        elif isinstance(value, float):
            if not math.isfinite(value) or value != int(value):
                raise ValueError(f"{value!r} is not an integer")
            whole = int(value)
        else:
            try:
                whole = int(str(value).strip())
            except ValueError:
                raise ValueError(f"{value!r} is not an integer") from None
        if not MIN_INTEGER <= whole <= MAX_INTEGER:
            raise ValueError(f"{whole} is outside the 64-bit integer range")
        return whole

    def python_type(self, config: NoConfig) -> TypeForm[Any]:
        return int


class DecimalConfig(BaseWireValueObject):
    model_config = ConfigDict(extra="forbid")

    precision: int = Field(default=18, ge=1)
    scale: int = Field(default=6, ge=0)

    @model_validator(mode="after")
    def _scale_fits(self) -> DecimalConfig:
        if self.scale > self.precision:
            raise ValueError("scale must not exceed precision")
        return self


class DecimalType(FieldTypeHandler[DecimalConfig]):
    type_id = FieldType.decimal
    config_model = DecimalConfig
    pg_cast = "numeric"
    capabilities = Capabilities(filter_ops=_ORDERED, sortable=True, index_kinds=_BTREE)

    def normalize(self, value: object, *, config: DecimalConfig) -> JsonValue:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError("a boolean is not a decimal")
        try:
            parsed = Decimal(str(value).strip())
        except InvalidOperation:
            raise ValueError(f"{value!r} is not a decimal") from None
        if not parsed.is_finite():
            raise ValueError(f"{value!r} is not a finite decimal")
        _, digits, exponent = parsed.normalize().as_tuple()
        if not isinstance(exponent, int):
            raise ValueError(f"{value!r} is not a finite decimal")
        if max(-exponent, 0) > config.scale:
            raise ValueError(f"more than {config.scale} decimal places")
        if max(len(digits) + exponent, 1) > config.precision - config.scale:
            raise ValueError(f"does not fit precision {config.precision}, scale {config.scale}")
        return str(parsed)

    def python_type(self, config: DecimalConfig) -> TypeForm[Any]:
        return annotated(Decimal, Field(max_digits=config.precision, decimal_places=config.scale))


_TRUE = {"true", "t", "1", "yes", "y", "on"}
_FALSE = {"false", "f", "0", "no", "n", "off"}


class BooleanType(FieldTypeHandler):
    type_id = FieldType.boolean
    config_model = NoConfig
    pg_cast = "boolean"
    capabilities = Capabilities(filter_ops=frozenset({FilterOp.eq, FilterOp.ne, FilterOp.is_null}), sortable=True)

    def normalize(self, value: object, *, config: NoConfig) -> JsonValue:
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str) and value.strip().lower() in _TRUE | _FALSE:
            return value.strip().lower() in _TRUE
        raise ValueError(f"{value!r} is not a boolean")

    def python_type(self, config: NoConfig) -> TypeForm[Any]:
        return bool


class DateType(FieldTypeHandler):
    type_id = FieldType.date
    config_model = NoConfig
    capabilities = Capabilities(
        filter_ops=_ORDERED - {FilterOp.in_, FilterOp.not_in}, sortable=True, index_kinds=_BTREE
    )

    def normalize(self, value: object, *, config: NoConfig) -> JsonValue:
        if value is None:
            return None
        if isinstance(value, datetime):
            return to_date_iso(value.date())
        if isinstance(value, date):
            return to_date_iso(value)
        if isinstance(value, str):
            return to_date_iso(from_date_iso(value))
        raise ValueError(f"{value!r} is not a date")

    def python_type(self, config: NoConfig) -> TypeForm[Any]:
        return date


class DateTimeType(FieldTypeHandler):
    type_id = FieldType.datetime
    config_model = NoConfig
    capabilities = Capabilities(
        filter_ops=_ORDERED - {FilterOp.in_, FilterOp.not_in}, sortable=True, index_kinds=_BTREE
    )

    def normalize(self, value: object, *, config: NoConfig) -> JsonValue:
        if value is None:
            return None
        if isinstance(value, str):
            value = from_iso(value)
        if isinstance(value, datetime):
            return to_iso(value)
        raise ValueError(f"{value!r} is not a datetime")

    def python_type(self, config: NoConfig) -> TypeForm[Any]:
        return datetime


def _uuid_text(value: object, what: str) -> str:
    if isinstance(value, uuid.UUID):
        return str(value)
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise ValueError(f"{value!r} is not {what}") from None


class UUIDType(FieldTypeHandler):
    type_id = FieldType.uuid
    config_model = NoConfig
    pg_cast = "uuid"
    capabilities = Capabilities(filter_ops=_EQUALITY, index_kinds=_BTREE)

    def normalize(self, value: object, *, config: NoConfig) -> JsonValue:
        return None if value is None else _uuid_text(value, "a UUID")

    def python_type(self, config: NoConfig) -> TypeForm[Any]:
        return uuid.UUID


class EnumConfig(BaseWireValueObject):
    model_config = ConfigDict(extra="forbid")

    options: tuple[str, ...] = Field(min_length=1)


class EnumType(FieldTypeHandler[EnumConfig]):
    type_id = FieldType.enum
    config_model = EnumConfig
    capabilities = Capabilities(filter_ops=_EQUALITY, sortable=True, index_kinds=_BTREE)

    def normalize(self, value: object, *, config: EnumConfig) -> JsonValue:
        if value is None:
            return None
        if value not in config.options:
            raise ValueError(f"{value!r} is not one of {list(config.options)}")
        return str(value)

    def python_type(self, config: EnumConfig) -> TypeForm[Any]:
        return one_of(config.options)


class JsonType(FieldTypeHandler):
    type_id = FieldType.json
    config_model = NoConfig
    capabilities = Capabilities()

    def normalize(self, value: object, *, config: NoConfig) -> JsonValue:
        if value is None:
            return None
        try:
            stored = json.loads(json.dumps(value, allow_nan=False))
        except TypeError, ValueError:
            raise ValueError("not a JSON value") from None
        if holds_nul(stored):
            raise ValueError("a JSON value may not contain a NUL character")
        return stored

    def python_type(self, config: NoConfig) -> TypeForm[Any]:
        return JsonValue


class RefConfig(BaseWireValueObject):
    model_config = ConfigDict(extra="forbid")

    table: str
    on_delete: OnDelete = OnDelete.restrict

    @model_validator(mode="after")
    def _slug(self) -> RefConfig:
        validate_slug(self.table)
        return self


class RefType(FieldTypeHandler[RefConfig]):
    type_id = FieldType.ref
    config_model = RefConfig
    pg_cast = "uuid"
    capabilities = Capabilities(filter_ops=_EQUALITY, index_kinds=_BTREE)

    def normalize(self, value: object, *, config: RefConfig) -> JsonValue:
        return None if value is None else _uuid_text(value, "a row id")

    def python_type(self, config: RefConfig) -> TypeForm[Any]:
        return uuid.UUID


BUILTINS: tuple[FieldTypeHandler[Any], ...] = (
    TextType(),
    IntegerType(),
    DecimalType(),
    BooleanType(),
    DateType(),
    DateTimeType(),
    UUIDType(),
    EnumType(),
    JsonType(),
    RefType(),
)

for _handler in BUILTINS:
    if not is_registered(_handler.type_id):
        register_field_type(_handler)

__all__ = ["BUILTINS", "DecimalConfig", "EnumConfig", "RefConfig", "RefType", "TextConfig"]
