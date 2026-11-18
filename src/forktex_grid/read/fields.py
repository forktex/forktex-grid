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
from typing import Any

import sqlalchemy as sa
from forktex.database.filters import FilterOp
from forktex.iso import to_iso
from forktex.types import JsonValue
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.orm.util import AliasedClass

from forktex_grid.domain.enums import Cardinality, FieldType
from forktex_grid.domain.fieldtypes import Capabilities, FieldTypeHandler, get_field_type
from forktex_grid.domain.query import SYSTEM_FIELDS
from forktex_grid.domain.resolved import ResolvedColumn, ResolvedTable
from forktex_grid.errors import BadRequestError
from forktex_grid.persist.casts import as_compared, compared_literal
from forktex_grid.persist.models import GridRow

type RowsEntity = type[GridRow] | AliasedClass[GridRow]


class QueryField(abc.ABC):
    key: str
    handler: FieldTypeHandler[Any]

    @property
    def capabilities(self) -> Capabilities:
        return self.handler.capabilities

    @property
    @abc.abstractmethod
    def nullable(self) -> bool: ...

    @abc.abstractmethod
    def compared(self) -> sa.ColumnElement[Any]: ...

    @abc.abstractmethod
    def text(self) -> sa.ColumnElement[Any]: ...

    @abc.abstractmethod
    def literal(self, value: object) -> sa.ColumnElement[Any]: ...

    @abc.abstractmethod
    def value_of(self, row: GridRow) -> JsonValue: ...

    def _stored(self, value: object, config: object) -> JsonValue:
        try:
            return self.handler.normalize(value, config=config)
        except ValueError as exc:
            raise BadRequestError(f"column '{self.key}': {exc}") from None


class ColumnField(QueryField):
    def __init__(self, column: ResolvedColumn, rows: RowsEntity) -> None:
        if column.spec.cardinality is Cardinality.many:
            raise BadRequestError(f"column '{column.key}' holds a list: it cannot be filtered or sorted")
        self.key = column.key
        self.handler = column.handler
        self._column = column
        self._rows = rows

    @property
    def nullable(self) -> bool:
        return not self._column.spec.is_required

    def text(self) -> sa.ColumnElement[Any]:
        return self._rows.payload[self.key].astext

    def compared(self) -> sa.ColumnElement[Any]:
        return as_compared(self.handler, self.text())

    def literal(self, value: object) -> sa.ColumnElement[Any]:
        return compared_literal(self.handler, self._stored(value, self._column.config))

    def value_of(self, row: GridRow) -> JsonValue:
        return row.payload.get(self.key)


_SYSTEM_TYPES: dict[str, FieldType] = {
    "id": FieldType.uuid,
    "external_ref": FieldType.text,
    "created_at": FieldType.datetime,
    "updated_at": FieldType.datetime,
}


class SystemField(QueryField):
    def __init__(self, key: str, rows: RowsEntity) -> None:
        self.key = key
        self.handler = get_field_type(_SYSTEM_TYPES[key])
        self._attribute: InstrumentedAttribute[Any] = getattr(rows, key)

    @property
    def capabilities(self) -> Capabilities:
        return self.handler.capabilities.model_copy(update={"sortable": True})

    @property
    def nullable(self) -> bool:
        return self.key == "external_ref"

    def text(self) -> sa.ColumnElement[Any]:
        return sa.cast(self._attribute, sa.Text)

    def compared(self) -> sa.ColumnElement[Any]:
        return self._attribute.expression

    def literal(self, value: object) -> sa.ColumnElement[Any]:
        stored = self._stored(value, self.handler.parse_config({}))
        return sa.cast(sa.literal(str(stored)), self._attribute.type)

    def value_of(self, row: GridRow) -> JsonValue:
        value = getattr(row, self.key)
        if value is None:
            return None
        return to_iso(value) if self.key in ("created_at", "updated_at") else str(value)


class FieldSource:
    def __init__(self, table: ResolvedTable, rows: RowsEntity = GridRow) -> None:
        self._table = table
        self._rows = rows

    def field(self, key: str) -> QueryField:
        if key in SYSTEM_FIELDS:
            return SystemField(key, self._rows)
        return ColumnField(self._table.column(key), self._rows)

    def check(self, column: str, op: FilterOp) -> None:
        field = self.field(column)
        if op not in field.capabilities.filter_ops:
            raise BadRequestError(f"column '{column}' ({field.handler.type_id}) does not take '{op}'")

    def raw_expr(self, column: str) -> sa.ColumnElement[Any]:
        return self.field(column).text()

    def typed_expr(self, column: str) -> sa.ColumnElement[Any]:
        return self.field(column).compared()

    def operand(self, column: str, value: object) -> sa.ColumnElement[Any]:
        return self.field(column).literal(value)

    def like_lhs(self, column: str) -> sa.ColumnElement[Any]:
        return self.field(column).text()


__all__ = ["ColumnField", "FieldSource", "QueryField", "RowsEntity", "SystemField"]
