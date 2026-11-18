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

from forktex.types import BaseValueObject
from pydantic import ConfigDict, Field, PlainSerializer, create_model
from typing_extensions import TypeForm

from forktex_grid.domain.enums import Cardinality
from forktex_grid.domain.fieldtypes import annotated, get_field_type, list_of, optional
from forktex_grid.domain.schema import ColumnSpec, TableSpec


class RowValues(BaseValueObject):
    model_config = ConfigDict(extra="forbid", validate_by_name=True, validate_by_alias=True, serialize_by_alias=True)


def _annotation(column: ColumnSpec) -> TypeForm[Any]:
    handler = get_field_type(column.type_id)
    config = handler.parse_config(column.config)
    stored_form = PlainSerializer(lambda value: handler.normalize(value, config=config), when_used="json")
    python = annotated(handler.python_type(config), stored_form)
    return list_of(python) if column.cardinality is Cardinality.many else python


def _field(column: ColumnSpec) -> tuple[Any, Any]:
    annotation = _annotation(column)
    if column.is_required:
        return annotation, Field(alias=column.key, title=column.label)
    return optional(annotation), Field(default=None, alias=column.key, title=column.label)


def _attribute(key: str) -> str:
    return f"{key}_" if hasattr(RowValues, key) else key


def row_model(spec: TableSpec) -> type[RowValues]:
    fields: dict[str, Any] = {_attribute(c.key): _field(c) for c in spec.columns}
    name = "".join(part.title() for part in spec.slug.split("_")) + "Values"
    return create_model(name, __base__=RowValues, __doc__=spec.label, **fields)


__all__ = ["RowValues", "row_model"]
