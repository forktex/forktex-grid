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
from collections.abc import Mapping

from forktex.types import JsonValue

from forktex_grid.domain.enums import Cardinality
from forktex_grid.domain.resolved import ResolvedColumn, ResolvedTable
from forktex_grid.errors import BadRequestError


def stored_value(col: ResolvedColumn, value: object) -> JsonValue:
    try:
        if col.spec.cardinality is Cardinality.many:
            if value is None:
                return None
            if not isinstance(value, list | tuple):
                raise ValueError("expected a list")
            return [col.handler.normalize(v, config=col.config) for v in value]
        return col.handler.normalize(value, config=col.config)
    except ValueError as exc:
        raise BadRequestError(f"column '{col.key}': {exc}", details={"column": col.key}) from None


def stored_payload(table: ResolvedTable, values: Mapping[str, object], *, creating: bool) -> dict[str, JsonValue]:
    unknown = sorted(set(values) - set(table.columns))
    if unknown:
        raise BadRequestError(f"table '{table.spec.slug}' has no column(s) {unknown}", details={"columns": unknown})
    out: dict[str, JsonValue] = {}
    for key, col in table.columns.items():
        if key in values:
            out[key] = stored_value(col, values[key])
        elif creating and col.spec.default_value is not None:
            out[key] = stored_value(col, col.spec.default_value)
        if col.spec.is_required and (creating or key in out) and out.get(key) is None:
            raise BadRequestError(f"column '{key}' is required", details={"column": key})
    return out


def linked_ids(stored: JsonValue) -> list[uuid.UUID]:
    if stored is None:
        return []
    values = stored if isinstance(stored, list) else [stored]
    return [uuid.UUID(str(v)) for v in values]


__all__ = ["linked_ids", "stored_payload", "stored_value"]
