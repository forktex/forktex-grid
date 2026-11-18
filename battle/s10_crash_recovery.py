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
import os
import subprocess
from collections.abc import Awaitable, Callable

import sqlalchemy as sa
from _consumer import Report, check, engine, session_of
from forktex.error import AppError
from sqlalchemy.ext.asyncio import AsyncEngine

from forktex_grid import (
    AlreadyExistsError,
    ApplyOptions,
    Batch,
    ColumnSpec,
    CreateRow,
    CursorPaging,
    IndexBuilds,
    IndexSpec,
    Migration,
    Namespace,
    Query,
    Schema,
    TableSource,
    TableSpec,
    apply_migrations,
)

CONTAINER = os.environ["BATTLE_POSTGRES_CONTAINER"]
ROWS = int(os.environ.get("BATTLE_CRASH_ROWS", "60000"))
WRITERS = 4
PLAIN = TableSpec(
    slug="order", columns=[ColumnSpec(key="code", type_id="text"), ColumnSpec(key="n", type_id="integer")]
)
INDEXED = PLAIN.model_copy(
    update={
        "columns": (ColumnSpec(key="code", type_id="text", is_unique=True), ColumnSpec(key="n", type_id="integer")),
        "indexes": (IndexSpec(column_keys=("n",)),),
    }
)
INVALID_GRID_INDEXES = sa.text(
    "SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
    "WHERE c.relname LIKE 'gx_%' AND NOT i.indisvalid"
)


def crash() -> None:
    subprocess.run(["docker", "exec", CONTAINER, "sh", "-c", "pkill -KILL -f 'postgres: postgres '"], check=False)


async def back_up() -> AsyncEngine:
    for _ in range(120):
        bind = engine(pool_size=8)
        try:
            async with bind.connect() as conn:
                await conn.execute(sa.text("SELECT 1"))
            return bind
        except Exception:
            await bind.dispose()
            await asyncio.sleep(0.25)
    raise SystemExit("postgres did not recover")


async def invalid_indexes(bind: AsyncEngine) -> int:
    async with bind.connect() as conn:
        return (await conn.execute(INVALID_GRID_INDEXES)).scalar_one()


def failure_kind(error: BaseException) -> str:
    check(not isinstance(error, AppError), f"a crash surfaced as a grid error: {error!r}")
    check(isinstance(error, (sa.exc.DBAPIError, OSError)), f"a crash surfaced as {type(error).__name__}: {error!r}")
    return type(error).__name__


async def guarded(work: Callable[[], Awaitable[None]], failures: list[str]) -> None:
    try:
        await work()
    except Exception as error:
        failures.append(failure_kind(error))


async def all_rows_valid(bind: AsyncEngine, namespace: str) -> int:
    async with session_of(bind) as s:
        table = await Namespace(s, namespace).table("order")
        model, seen, after = table.model(), 0, None
        while True:
            page = await table.query(Query(page=CursorPaging(size=1000, after=after)))
            for row in page.items:
                model.model_validate(row.values)
                seen += 1
            if page.next_cursor is None:
                return seen
            after = page.next_cursor


async def populated(bind: AsyncEngine) -> None:
    async with session_of(bind) as s:
        ns = Namespace(s, "shop")
        await ns.apply(Schema(tables=[PLAIN]), ApplyOptions(index_builds=IndexBuilds.inline))
        table = await ns.table("order")
        for start in range(0, ROWS, 5000):
            await table.create_many([{"code": f"C{j}", "n": j % 977} for j in range(start, start + 5000)])
        await s.commit()


async def crash_during_an_index_build_with_writers(bind: AsyncEngine, report: Report) -> AsyncEngine:
    acked: list[str] = []
    failures: list[str] = []
    stop = asyncio.Event()

    async def writer(k: int) -> None:
        j = 0
        while not stop.is_set():
            async with session_of(bind) as s:
                await (await Namespace(s, "shop").table("order")).create({"code": f"W{k}-{j}", "n": j})
                await s.commit()
            acked.append(f"W{k}-{j}")
            j += 1

    async def applier() -> None:
        async with session_of(bind) as s:
            await Namespace(s, "shop").apply(Schema(tables=[INDEXED]), ApplyOptions(allow_destructive=True))

    snapshot = await (await bind.connect()).execution_options(isolation_level="REPEATABLE READ")
    await snapshot.execute(sa.text("SELECT 1"))
    tasks = [asyncio.create_task(guarded(lambda k=k: writer(k), failures)) for k in range(WRITERS)]
    tasks.append(asyncio.create_task(guarded(applier, failures)))
    for _ in range(600):
        if await invalid_indexes(bind):
            break
        await asyncio.sleep(0.05)
    check(await invalid_indexes(bind) > 0, "the concurrent index build never started")
    for _ in range(600):
        if len(acked) >= 50:
            break
        await asyncio.sleep(0.05)
    check(len(acked) >= 50, f"writers committed {len(acked)} rows while the index was being built")
    crash()
    stop.set()
    await asyncio.wait(tasks, timeout=60)
    report.set("crash_1_failures", sorted(set(failures)))
    report.set("crash_1_acked_writes", len(acked))
    await bind.dispose()

    bind = await back_up()
    report.set("crash_1_invalid_indexes_after_recovery", await invalid_indexes(bind))
    async with session_of(bind) as s:
        table = await Namespace(s, "shop").table("order")
        stored = {
            r["code"]
            for r in (
                await table.query(
                    Query(where={"column": "code", "op": "in", "value": acked}, page=CursorPaging(size=len(acked)))
                )
            ).items
        }
    check(stored == set(acked), f"{len(set(acked) - stored)} acknowledged writes lost in the crash")
    async with session_of(bind) as s:
        again = await Namespace(s, "shop").apply(Schema(tables=[INDEXED]), ApplyOptions(allow_destructive=True))
    check(again.complete, f"the next apply did not finish the indexes: {again.indexes}")
    check(await invalid_indexes(bind) == 0, "an invalid index survived the next apply")
    async with session_of(bind) as s:
        try:
            await (await Namespace(s, "shop").table("order")).create({"code": "C7"})
            check(False, "uniqueness not enforced after the rebuild")
        except AlreadyExistsError:
            pass
    report.set("crash_1_rows_valid", await all_rows_valid(bind, "shop"))
    return bind


async def crash_during_a_migration(bind: AsyncEngine, report: Report) -> AsyncEngine:
    migration = Migration(desired=Schema(tables=[INDEXED]), tables=[TableSource(target="order", source="order")])
    failures: list[str] = []

    async def migrating() -> None:
        async with session_of(bind) as s:
            await Namespace(s, "shop").migrate_to(Namespace(s, "shop-v2"), migration)
            await s.commit()

    task = asyncio.create_task(guarded(migrating, failures))
    await asyncio.sleep(1.0)
    check(not task.done(), "the migration finished before the crash: raise BATTLE_CRASH_ROWS")
    crash()
    await asyncio.wait([task], timeout=60)
    report.set("crash_2_failures", sorted(set(failures)))
    await bind.dispose()

    bind = await back_up()
    async with session_of(bind) as s:
        left = await Namespace(s, "shop-v2").row_counts()
        check(left == {}, f"a migration killed halfway left {left}")
        source = (await Namespace(s, "shop").row_counts())["order"]
        done = await Namespace(s, "shop").migrate_to(Namespace(s, "shop-v2"), migration)
        await s.commit()
    check(done.tables[0].rows == source, f"the second migration carried {done.tables[0].rows} of {source}")
    report.set("crash_2_rows_carried_after", done.tables[0].rows)
    return bind


async def crash_during_a_batch(bind: AsyncEngine, report: Report) -> AsyncEngine:
    failures: list[str] = []
    written = asyncio.Event()

    async def batching() -> None:
        async with session_of(bind) as s:
            rows = [CreateRow(table="order", values={"code": f"B{i}", "n": i}) for i in range(5000)]
            await Namespace(s, "seeded").batch(Batch(desired=Schema(tables=[PLAIN]), rows=rows))
            written.set()
            await asyncio.sleep(30)
            await s.commit()

    task = asyncio.create_task(guarded(batching, failures))
    await asyncio.wait_for(written.wait(), timeout=60)
    crash()
    await asyncio.wait([task], timeout=60)
    report.set("crash_3_failures", sorted(set(failures)))
    await bind.dispose()

    bind = await back_up()
    async with session_of(bind) as s:
        left = await Namespace(s, "seeded").row_counts()
    check(left == {}, f"a batch killed before its commit left {left}")
    return bind


async def main() -> None:
    report = Report()
    bind = engine(pool_size=8)
    await apply_migrations(bind)
    await populated(bind)
    bind = await crash_during_an_index_build_with_writers(bind, report)
    bind = await crash_during_a_migration(bind, report)
    bind = await crash_during_a_batch(bind, report)
    check(await invalid_indexes(bind) == 0, "an invalid index at the end")
    report.emit()
    await bind.dispose()


if __name__ == "__main__":
    asyncio.run(main())
