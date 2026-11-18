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

import abc
import operator
from typing import Annotated, Any, ClassVar, Literal, cast

from forktex.database.filters import FilterOp
from forktex.types import BaseValueObject, BaseWireValueObject, JsonObject, JsonValue
from pydantic import ConfigDict, Field, TypeAdapter, ValidationError
from typing_extensions import TypeForm

from forktex_grid.domain.limits import MAX_TYPE_ID, storable_text
from forktex_grid.errors import AlreadyExistsError, BadRequestError

type IndexKind = Literal["btree", "trgm"]
type PgCast = Literal["bigint", "numeric", "boolean", "uuid"]


def _type_form(built_at_runtime: object) -> TypeForm[Any]:
    return cast("TypeForm[Any]", built_at_runtime)


def list_of(item: TypeForm[Any]) -> TypeForm[Any]:
    return _type_form(list.__class_getitem__(item))


def optional(item: TypeForm[Any]) -> TypeForm[Any]:
    return _type_form(operator.or_(item, None))


def one_of(options: tuple[str, ...]) -> TypeForm[Any]:
    return _type_form(Literal[*options])


def annotated(item: TypeForm[Any], *metadata: object) -> TypeForm[Any]:
    return _type_form(Annotated[item, *metadata])


class Capabilities(BaseWireValueObject):
    filter_ops: frozenset[FilterOp] = Field(default_factory=frozenset)
    sortable: bool = False
    index_kinds: frozenset[IndexKind] = Field(default_factory=frozenset)


class NoConfig(BaseWireValueObject):
    model_config = ConfigDict(extra="forbid")


class FieldTypeHandler[C: BaseValueObject = NoConfig](abc.ABC):
    type_id: ClassVar[str]
    capabilities: ClassVar[Capabilities]
    config_model: type[C]
    pg_cast: ClassVar[PgCast | None] = None

    def parse_config(self, raw: JsonObject) -> C:
        return self.config_model.model_validate(raw)

    @abc.abstractmethod
    def normalize(self, value: object, *, config: C) -> JsonValue: ...

    def python_type(self, config: C) -> TypeForm[Any]:
        return Any


class AnnotatedFieldType(FieldTypeHandler[NoConfig]):
    annotation: ClassVar[TypeForm[Any]]
    config_model = NoConfig

    def __init__(self) -> None:
        self._adapter: TypeAdapter[Any] = TypeAdapter(self.annotation)

    def normalize(self, value: object, *, config: NoConfig) -> JsonValue:
        if value is None:
            return None
        try:
            return self._adapter.dump_python(self._adapter.validate_python(value), mode="json")
        except ValidationError as exc:
            raise ValueError(exc.errors()[0]["msg"]) from None

    def python_type(self, config: NoConfig) -> TypeForm[Any]:
        return self.annotation


_EQUALITY_OPS = frozenset({FilterOp.eq, FilterOp.ne, FilterOp.in_, FilterOp.not_in, FilterOp.is_null})


def field_type(
    type_id: str,
    annotation: TypeForm[Any],
    *,
    capabilities: Capabilities | None = None,
    pg_cast: PgCast | None = None,
) -> FieldTypeHandler[NoConfig]:
    namespace = {
        "type_id": type_id,
        "annotation": annotation,
        "capabilities": capabilities or Capabilities(filter_ops=_EQUALITY_OPS, index_kinds=frozenset({"btree"})),
        "pg_cast": pg_cast,
    }
    return type(f"{type_id.title()}FieldType", (AnnotatedFieldType,), namespace)()


_REGISTRY: dict[str, FieldTypeHandler[Any]] = {}


def register_field_type(handler: FieldTypeHandler[Any], *, replace: bool = False) -> None:
    if not handler.type_id:
        raise BadRequestError("a field type needs a type_id")
    storable_text(handler.type_id, "a type_id", max_length=MAX_TYPE_ID)
    if handler.type_id in _REGISTRY and not replace:
        raise AlreadyExistsError(f"field type {handler.type_id!r} is already registered")
    _REGISTRY[handler.type_id] = handler


def get_field_type(type_id: str) -> FieldTypeHandler[Any]:
    try:
        return _REGISTRY[type_id]
    except KeyError:
        raise BadRequestError(f"unknown field type '{type_id}'") from None


def is_registered(type_id: str) -> bool:
    return type_id in _REGISTRY


def all_field_types() -> dict[str, FieldTypeHandler[Any]]:
    return dict(_REGISTRY)


__all__ = [
    "AnnotatedFieldType",
    "Capabilities",
    "FieldTypeHandler",
    "FilterOp",
    "IndexKind",
    "NoConfig",
    "PgCast",
    "all_field_types",
    "annotated",
    "field_type",
    "get_field_type",
    "is_registered",
    "list_of",
    "one_of",
    "optional",
    "register_field_type",
]
