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

from forktex.types import JsonValue

from forktex_grid.errors import BadRequestError

MAX_NAME = 63
MAX_TYPE_ID = 64
MAX_LABEL = 255
MAX_EXTERNAL_REF = 255
MIN_INTEGER = -(2**63)
MAX_INTEGER = 2**63 - 1


def storable_text(text: str, what: str, *, max_length: int | None = None) -> str:
    if "\x00" in text:
        raise BadRequestError(f"{what} may not contain a NUL character")
    if max_length is not None and len(text) > max_length:
        raise BadRequestError(f"{what} is longer than {max_length} characters")
    return text


def holds_nul(value: JsonValue) -> bool:
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, list):
        return any(holds_nul(v) for v in value)
    if isinstance(value, dict):
        return any("\x00" in k or holds_nul(v) for k, v in value.items())
    return False


__all__ = [
    "MAX_EXTERNAL_REF",
    "MAX_INTEGER",
    "MAX_LABEL",
    "MAX_NAME",
    "MAX_TYPE_ID",
    "MIN_INTEGER",
    "holds_nul",
    "storable_text",
]
