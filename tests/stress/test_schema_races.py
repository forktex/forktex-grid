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

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from forktex_grid import (
    ApplyOptions,
    BadRequestError,
    ColumnSpec,
    IndexBuilds,
    Migration,
    Namespace,
    Schema,
    TableSource,
    TableSpec,
)
from tests.conftest import declare

TIMEOUT = 30
STATUS = TableSpec(
    slug="task", columns=(ColumnSpec(key="status", type_id="enum", config={"options": ["todo", "done"]}),)
)
STRICT = TableSpec(
    slug="task",
    columns=(ColumnSpec(key="status", type_id="enum", is_required=True, config={"options": ["todo"]}),),
)
TIGHTEN = ApplyOptions(allow_destructive=True)


async def test_a_write_through_a_table_opened_before_a_schema_change_meets_the_new_schema(
    grid_engine: AsyncEngine,
) -> None:
    maker = async_sessionmaker(bind=grid_engine, expire_on_commit=False)
    async with maker() as setup:
        await declare(Namespace(setup, "race"), STATUS)
        await setup.commit()
    async with maker() as writer, maker() as changer:
        tasks = await Namespace(writer, "race").table("task")
        await writer.commit()
        await Namespace(changer, "race").apply(Schema(tables=(STRICT,)), TIGHTEN)
        with pytest.raises(BadRequestError, match="'done'"):
            await tasks.create({"status": "done"})
        await writer.rollback()
        with pytest.raises(BadRequestError, match="'status' is required"):
            await tasks.create({})
        await writer.rollback()
        assert (await tasks.create({"status": "todo"}))["status"] == "todo"
        await writer.commit()


async def test_a_schema_change_waits_for_a_write_in_flight_and_then_sees_it(grid_engine: AsyncEngine) -> None:
    maker = async_sessionmaker(bind=grid_engine, expire_on_commit=False)
    async with maker() as setup:
        await declare(Namespace(setup, "inflight"), STATUS)
        await setup.commit()
    async with maker() as writer, maker() as changer:
        await (await Namespace(writer, "inflight").table("task")).create({"status": "done"})
        tightening = asyncio.create_task(Namespace(changer, "inflight").apply(Schema(tables=(STRICT,)), TIGHTEN))
        await asyncio.sleep(0.3)
        assert not tightening.done()
        await writer.commit()
        with pytest.raises(BadRequestError, match="1 existing row"):
            await asyncio.wait_for(tightening, TIMEOUT)


async def test_concurrent_applies_in_one_process_never_wait_on_each_others_connections(
    grid_engine: AsyncEngine,
) -> None:
    tight = create_async_engine(
        grid_engine.url, execution_options=grid_engine.get_execution_options(), pool_size=2, max_overflow=0
    )
    maker = async_sessionmaker(bind=tight, expire_on_commit=False)

    async def apply(namespace: str) -> None:
        async with maker() as s:
            await Namespace(s, namespace).apply(Schema(tables=(STATUS,)))

    try:
        await asyncio.wait_for(asyncio.gather(*(apply(f"pool-{n}") for n in range(4))), TIMEOUT)
    finally:
        await tight.dispose()


async def test_a_migration_dry_run_releases_the_target_when_it_rolls_back(grid_engine: AsyncEngine) -> None:
    maker = async_sessionmaker(bind=grid_engine, expire_on_commit=False)
    async with maker() as setup:
        await declare(Namespace(setup, "from"), STATUS)
        await setup.commit()
    copy = Migration(desired=Schema(tables=(STATUS,)), tables=(TableSource(target="task", source="task"),))
    async with maker() as rehearsing, maker() as other:
        await Namespace(rehearsing, "from").migrate_to(Namespace(rehearsing, "to"), copy, ApplyOptions(dry_run=True))
        inline = ApplyOptions(index_builds=IndexBuilds.inline)
        await asyncio.wait_for(Namespace(other, "to").apply(Schema(tables=(STATUS,)), inline), TIMEOUT)
        await other.commit()
        await rehearsing.rollback()


async def test_a_migration_holds_writes_to_its_source_until_it_commits(grid_engine: AsyncEngine) -> None:
    maker = async_sessionmaker(bind=grid_engine, expire_on_commit=False)
    async with maker() as setup:
        await declare(Namespace(setup, "busy"), STATUS)
        await setup.commit()
    copy = Migration(desired=Schema(tables=(STATUS,)), tables=(TableSource(target="task", source="task"),))
    async with maker() as migrating, maker() as writer:
        tasks = await Namespace(writer, "busy").table("task")
        await writer.commit()
        await Namespace(migrating, "busy").migrate_to(Namespace(migrating, "quiet"), copy)
        write = asyncio.create_task(tasks.create({"status": "todo"}))
        await asyncio.sleep(0.3)
        assert not write.done()
        await migrating.commit()
        await asyncio.wait_for(write, TIMEOUT)
        await writer.commit()


async def test_a_rename_of_the_target_key_waits_for_writes(session: AsyncSession) -> None:
    ns = Namespace(session, "renamed")
    (tasks,) = await declare(ns, STATUS)
    await tasks.create({"status": "todo"})
    await ns.rename_column("task", "status", "state")
    assert (await (await ns.table("task")).query()).items[0]["state"] == "todo"
