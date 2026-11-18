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

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_grid.errors import BadRequestError

TRGM = "pg_trgm"


_INSTALLED = sa.text("SELECT 1 FROM pg_extension WHERE extname = :name")
_VISIBLE = sa.text(
    "SELECT EXISTS (SELECT 1 FROM pg_opclass WHERE opcname = 'gin_trgm_ops' AND pg_opclass_is_visible(oid))"
    " AND EXISTS (SELECT 1 FROM pg_operator WHERE oprname = '%' AND oprleft = 'text'::regtype"
    " AND oprright = 'text'::regtype AND pg_operator_is_visible(oid))"
)


async def require_trgm(session: AsyncSession, needed_for: str) -> None:
    if await session.scalar(_INSTALLED, {"name": TRGM}) is None:
        raise BadRequestError(
            f"{needed_for} needs the {TRGM} extension, which this database does not have; "
            f"an administrator installs it with CREATE EXTENSION {TRGM}"
        )
    if not await session.scalar(_VISIBLE):
        raise BadRequestError(
            f"{needed_for} needs the {TRGM} extension on the search path; it is installed in a schema "
            "the connection does not search: add that schema to search_path, or reinstall it in one it does"
        )


__all__ = ["TRGM", "require_trgm"]
