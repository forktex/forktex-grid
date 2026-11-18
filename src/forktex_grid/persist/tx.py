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

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from forktex.database import savepoint
from forktex.database.integrity import concurrency_boundary, integrity_boundary, sqlstate
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_grid.errors import BadRequestError

_PROGRAM_LIMIT_EXCEEDED = "54000"


@asynccontextmanager
async def _limits_boundary() -> AsyncIterator[None]:
    try:
        yield
    except DBAPIError as exc:
        if sqlstate(exc) != _PROGRAM_LIMIT_EXCEEDED:
            raise
        raise BadRequestError("a value is too large for Postgres to index; shorten it or drop the index") from None


@asynccontextmanager
async def atomic(session: AsyncSession) -> AsyncIterator[None]:
    async with concurrency_boundary(), integrity_boundary(), _limits_boundary(), savepoint(session):
        yield


@asynccontextmanager
async def rehearsal(session: AsyncSession) -> AsyncIterator[None]:
    nested = await session.begin_nested()
    try:
        yield
    finally:
        if nested.is_active:
            await nested.rollback()


__all__ = ["atomic", "rehearsal"]
