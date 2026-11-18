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
from datetime import datetime

from forktex.database.pagination import Page as SubstratePage
from forktex.types import BaseWireValueObject, JsonObject, JsonValue
from pydantic import Field

from forktex_grid.domain.limits import MAX_EXTERNAL_REF, storable_text

type ExternalRef = str | int | uuid.UUID


def external_ref_text(value: ExternalRef | None) -> str | None:
    if value is None:
        return None
    return storable_text(str(value), "an external_ref", max_length=MAX_EXTERNAL_REF)


class Row(BaseWireValueObject):
    id: uuid.UUID
    external_ref: str | None = None
    values: JsonObject
    created_at: datetime
    updated_at: datetime
    links: dict[str, Row | tuple[Row, ...] | None] = Field(default_factory=dict)

    def __getitem__(self, key: str) -> JsonValue:
        return self.values[key]

    def get(self, key: str, default: JsonValue = None) -> JsonValue:
        return self.values.get(key, default)


Page = SubstratePage[Row]


class ValueCount(BaseWireValueObject):
    value: JsonValue
    count: int


class ValueCounts(BaseWireValueObject):
    groups: tuple[ValueCount, ...]
    others: int = 0


__all__ = ["ExternalRef", "Page", "Row", "ValueCount", "ValueCounts", "external_ref_text"]
