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

from forktex.types import BaseValueObject

from forktex_grid.domain.fieldtypes import FieldTypeHandler, get_field_type
from forktex_grid.domain.schema import ColumnSpec, TableSpec
from forktex_grid.errors import BadRequestError


class ResolvedColumn:
    __slots__ = ("config", "handler", "spec")

    def __init__(self, spec: ColumnSpec) -> None:
        self.spec = spec
        self.handler: FieldTypeHandler[Any] = get_field_type(spec.type_id)
        self.config: BaseValueObject = self.handler.parse_config(spec.config)

    @property
    def key(self) -> str:
        return self.spec.key


type SpecVersion = tuple[object, ...]


class ResolvedTable:
    __slots__ = ("columns", "id", "namespace", "spec", "version")

    def __init__(self, id: uuid.UUID, namespace: str, spec: TableSpec, version: SpecVersion = ()) -> None:
        self.id = id
        self.namespace = namespace
        self.spec = spec
        self.version = version
        self.columns: dict[str, ResolvedColumn] = {c.key: ResolvedColumn(c) for c in spec.columns}

    def column(self, key: str) -> ResolvedColumn:
        try:
            return self.columns[key]
        except KeyError:
            raise BadRequestError(f"table '{self.spec.slug}' has no column '{key}'") from None


__all__ = ["ResolvedColumn", "ResolvedTable", "SpecVersion"]
