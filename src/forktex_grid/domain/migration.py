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

from forktex.types import BaseWireValueObject, JsonObject
from pydantic import ConfigDict, Field, model_validator

from forktex_grid.domain.schema import Schema
from forktex_grid.errors import BadRequestError


class TableSource(BaseWireValueObject):
    model_config = ConfigDict(extra="forbid")

    target: str
    source: str
    columns: dict[str, str] | None = None
    constants: JsonObject = Field(default_factory=dict)


class Migration(BaseWireValueObject):
    model_config = ConfigDict(extra="forbid")

    desired: Schema
    tables: tuple[TableSource, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _targets_declared(self) -> Migration:
        declared = {t.slug for t in self.desired.tables}
        if unknown := sorted({t.target for t in self.tables} - declared):
            raise BadRequestError(f"tables {unknown} are filled but not declared in `desired`")
        return self


__all__ = ["Migration", "TableSource"]
