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
from collections.abc import Awaitable, Callable

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from forktex_grid import (
    AlreadyExistsError,
    ApplyOptions,
    ApplyReport,
    ColumnSpec,
    IndexBuilds,
    Namespace,
    Schema,
    ServiceUnavailableError,
    TableSpec,
)
from forktex_grid.persist.locks import polled_advisory_lock
from tests.conftest import declare

TIMEOUT = 30


def _schema(slug: str) -> Schema:
    return Schema(tables=[TableSpec(slug=slug, columns=[ColumnSpec(key="code", type_id="text", is_unique=True)])])


async def _in_transaction[T](
    maker: async_sessionmaker[AsyncSession], work: Callable[[AsyncSession], Awaitable[T]]
) -> T:
    async with maker() as s:
        result = await work(s)
        await s.commit()
        return result


async def test_inline_applies_in_two_namespaces_do_not_deadlock(grid_engine: AsyncEngine) -> None:
    maker = async_sessionmaker(bind=grid_engine, expire_on_commit=False)

    async def apply_and_write(ns: str) -> None:
        async def work(s: AsyncSession) -> None:
            namespace = Namespace(s, ns)
            await namespace.apply(_schema("item"), ApplyOptions(index_builds=IndexBuilds.inline))
            await (await namespace.table("item")).create({"code": ns})

        await _in_transaction(maker, work)

    await asyncio.wait_for(asyncio.gather(apply_and_write("a"), apply_and_write("b")), TIMEOUT)
    async with maker() as s:
        for ns in ("a", "b"):
            assert [r["code"] for r in (await (await Namespace(s, ns).table("item")).query()).items] == [ns]


async def test_applies_to_one_namespace_take_turns(grid_engine: AsyncEngine) -> None:
    maker = async_sessionmaker(bind=grid_engine, expire_on_commit=False)

    async def apply(s: AsyncSession) -> ApplyReport:
        return await Namespace(s, "a").apply(_schema("item"), ApplyOptions(index_builds=IndexBuilds.inline))

    first, second = await asyncio.wait_for(
        asyncio.gather(_in_transaction(maker, apply), _in_transaction(maker, apply)), TIMEOUT
    )
    assert sorted(len(r.plan.changes) for r in (first, second)) == [0, 1]


async def test_racing_upserts_of_one_host_key_make_one_row(grid_engine: AsyncEngine) -> None:
    maker = async_sessionmaker(bind=grid_engine, expire_on_commit=False)
    spec = TableSpec(slug="ext", extends="public.client", columns=[ColumnSpec(key="tier", type_id="text")])
    async with maker() as s:
        await declare(Namespace(s, "a"), spec)
        await s.commit()

    async def upsert(tier: str) -> None:
        async def work(s: AsyncSession) -> None:
            ext = await Namespace(s, "a").table("ext")
            await ext.upsert_by_external_ref(42, values={"tier": tier})

        await _in_transaction(maker, work)

    await asyncio.wait_for(asyncio.gather(upsert("gold"), upsert("silver")), TIMEOUT)
    async with maker() as s:
        rows = (await (await Namespace(s, "a").table("ext")).query()).items
        assert len(rows) == 1 and rows[0].external_ref == "42" and rows[0]["tier"] in {"gold", "silver"}


async def test_concurrent_applies_with_concurrent_index_builds_do_not_hang(grid_engine: AsyncEngine) -> None:
    roomy = create_async_engine(
        grid_engine.url, execution_options=grid_engine.get_execution_options(), pool_size=8, max_overflow=0
    )
    maker = async_sessionmaker(bind=roomy, expire_on_commit=False)
    spec = Schema(tables=[TableSpec(slug="item", columns=[ColumnSpec(key="code", type_id="text", is_unique=True)])])

    async def apply(s: AsyncSession) -> ApplyReport:
        return await Namespace(s, "a").apply(spec)

    reports = await asyncio.wait_for(asyncio.gather(*(_in_transaction(maker, apply) for _ in range(3))), TIMEOUT)
    assert sorted(len(r.plan.changes) for r in reports) == [0, 0, 1]
    assert all(r.complete for r in reports)
    await roomy.dispose()


async def test_a_lock_released_from_another_server_connection_is_reported(grid_engine: AsyncEngine) -> None:
    with pytest.raises(ServiceUnavailableError, match="transaction mode"):
        async with polled_advisory_lock(grid_engine, 4242) as conn:
            await conn.execute(sa.select(sa.func.pg_advisory_unlock(4242)))
    async with grid_engine.connect() as conn:
        held = await conn.execute(sa.text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"))
        assert held.scalar_one() == 0


async def test_an_error_inside_the_lock_is_raised_as_it_is(grid_engine: AsyncEngine) -> None:
    with pytest.raises(ZeroDivisionError):
        async with polled_advisory_lock(grid_engine, 4243):
            raise ZeroDivisionError


async def test_a_connection_lost_inside_the_lock_raises_the_loss_not_the_unlock(grid_engine: AsyncEngine) -> None:
    with pytest.raises(sa.exc.DBAPIError):
        async with polled_advisory_lock(grid_engine, 4244) as conn:
            pid = (await conn.execute(sa.select(sa.func.pg_backend_pid()))).scalar_one()
            async with grid_engine.connect() as other:
                await other.execute(sa.select(sa.func.pg_terminate_backend(pid)))
            await conn.execute(sa.select(1))
    async with grid_engine.connect() as conn:
        held = await conn.execute(sa.text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"))
        assert held.scalar_one() == 0


async def test_a_connection_lost_mid_index_build_is_raised_and_the_next_apply_rebuilds(
    grid_engine: AsyncEngine,
) -> None:
    roomy = create_async_engine(
        grid_engine.url, execution_options=grid_engine.get_execution_options(), pool_size=4, max_overflow=0
    )
    maker = async_sessionmaker(bind=roomy, expire_on_commit=False)
    plain = TableSpec(slug="item", columns=[ColumnSpec(key="code", type_id="text")])
    async with maker() as s:
        await declare(Namespace(s, "a"), plain)
        await (await Namespace(s, "a").table("item")).create({"code": "x"})
        await s.commit()

    async def apply(s: AsyncSession) -> ApplyReport:
        return await Namespace(s, "a").apply(_schema("item"), ApplyOptions(allow_destructive=True))

    snapshot = await (await roomy.connect()).execution_options(isolation_level="REPEATABLE READ")
    await snapshot.execute(sa.select(1))
    building = asyncio.create_task(_in_transaction(maker, apply))
    async with roomy.connect() as conn:
        watcher = await conn.execution_options(isolation_level="AUTOCOMMIT")
        for _ in range(TIMEOUT * 20):
            pid = (
                await watcher.execute(
                    sa.text("SELECT pid FROM pg_stat_activity WHERE query LIKE 'CREATE UNIQUE INDEX CONCURRENTLY%'")
                )
            ).scalar()
            if pid:
                await watcher.execute(sa.select(sa.func.pg_terminate_backend(pid)))
                break
            await asyncio.sleep(0.05)
    with pytest.raises(sa.exc.DBAPIError):
        await asyncio.wait_for(building, TIMEOUT)
    await snapshot.close()

    report = await asyncio.wait_for(_in_transaction(maker, apply), TIMEOUT)
    assert report.complete and [i.ok for i in report.indexes] == [True]
    async with maker() as s:
        with pytest.raises(AlreadyExistsError):
            await (await Namespace(s, "a").table("item")).create({"code": "x"})
    await roomy.dispose()
