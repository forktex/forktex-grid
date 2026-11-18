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
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from forktex.types import JsonValue

from forktex_grid.domain.fieldtypes import FieldTypeHandler, PgCast

_CASTS: dict[PgCast, tuple[sa.types.TypeEngine[Any], Callable[[Any], object]]] = {
    "bigint": (sa.BigInteger(), int),
    "numeric": (sa.Numeric(), lambda value: Decimal(str(value))),
    "boolean": (sa.Boolean(), bool),
    "uuid": (sa.UUID(), lambda value: uuid.UUID(str(value))),
}


def as_compared(handler: FieldTypeHandler[Any], stored_text: sa.ColumnElement[Any]) -> sa.ColumnElement[Any]:
    return stored_text if handler.pg_cast is None else sa.cast(stored_text, _CASTS[handler.pg_cast][0])


def compared_literal(handler: FieldTypeHandler[Any], stored: JsonValue) -> sa.ColumnElement[Any]:
    if handler.pg_cast is None:
        return sa.literal(str(stored), sa.Text())
    sql_type, to_python = _CASTS[handler.pg_cast]
    return sa.literal(to_python(stored), sql_type)


__all__ = ["as_compared", "compared_literal"]
