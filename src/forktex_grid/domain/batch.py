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
from typing import Annotated, Any, Literal

from forktex.types import BaseWireValueObject, JsonObject
from pydantic import ConfigDict, Field

from forktex_grid.domain.row import ExternalRef
from forktex_grid.domain.schema import Schema


def _op_required(schema: dict[str, Any]) -> None:
    schema["properties"]["op"].pop("default", None)
    schema["required"] = ["op", *schema.get("required", [])]


class _RowWrite(BaseWireValueObject):
    model_config = ConfigDict(extra="forbid", json_schema_extra=_op_required)

    table: str


class CreateRow(_RowWrite):
    op: Literal["create"] = "create"
    values: JsonObject
    external_ref: ExternalRef | None = None


class UpsertRow(_RowWrite):
    op: Literal["upsert"] = "upsert"
    external_ref: ExternalRef
    values: JsonObject


class PatchRow(_RowWrite):
    op: Literal["patch"] = "patch"
    row_id: uuid.UUID
    values: JsonObject


class ArchiveRow(_RowWrite):
    op: Literal["archive"] = "archive"
    row_id: uuid.UUID


type RowOp = Annotated[CreateRow | UpsertRow | PatchRow | ArchiveRow, Field(discriminator="op")]


class Batch(BaseWireValueObject):
    model_config = ConfigDict(extra="forbid")

    desired: Schema | None = None
    rows: tuple[RowOp, ...] = ()


__all__ = ["ArchiveRow", "Batch", "CreateRow", "PatchRow", "RowOp", "UpsertRow"]
