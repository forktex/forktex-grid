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

import asyncio
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import sqlalchemy as sa
from forktex.database.locks import advisory_key
from forktex.error import ServiceUnavailableError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

FIRST_RETRY_S = 0.01
LONGEST_RETRY_S = 0.5


@asynccontextmanager
async def polled_advisory_lock(engine: AsyncEngine, key: int) -> AsyncIterator[AsyncConnection]:
    async with engine.connect() as conn:
        idle = await conn.execution_options(isolation_level="AUTOCOMMIT")
        retry = FIRST_RETRY_S
        while not (await idle.execute(sa.select(sa.func.pg_try_advisory_lock(key)))).scalar_one():
            await asyncio.sleep(retry)
            retry = min(retry * 2, LONGEST_RETRY_S)
        try:
            yield idle
        except BaseException:
            if not idle.invalidated:
                await idle.execute(sa.select(sa.func.pg_advisory_unlock(key)))
            raise
        if not (await idle.execute(sa.select(sa.func.pg_advisory_unlock(key)))).scalar_one():
            raise ServiceUnavailableError(
                "grid's schema lock was taken on one database server connection and released from another, "
                "so its connections go through a pooler in transaction mode. The change is committed, but the "
                "lock stays held on that server connection until the pooler closes it, and the next schema "
                "change of this namespace waits for it. Point grid's engine at Postgres or at a session-mode pool"
            )


_TURNS: weakref.WeakKeyDictionary[sa.Engine, asyncio.Lock] = weakref.WeakKeyDictionary()


@asynccontextmanager
async def engine_turn(engine: sa.Engine) -> AsyncIterator[None]:
    turn = _TURNS.setdefault(engine, asyncio.Lock())
    async with turn:
        yield


def _schema_key(namespace: str) -> int:
    return advisory_key("grid.schema", namespace)


async def schema_shared(session: AsyncSession, namespace: str) -> None:
    await session.execute(sa.select(sa.func.pg_advisory_xact_lock_shared(_schema_key(namespace))))


async def schema_exclusive(session: AsyncSession, namespace: str) -> None:
    await session.execute(sa.select(sa.func.pg_advisory_xact_lock(_schema_key(namespace))))


__all__ = ["engine_turn", "polled_advisory_lock", "schema_exclusive", "schema_shared"]
