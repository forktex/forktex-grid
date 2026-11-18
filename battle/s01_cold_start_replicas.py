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
import subprocess
import sys
import time

import sqlalchemy as sa
from _consumer import Report, check, engine, session_of

from forktex_grid import (
    AlreadyExistsError,
    ColumnSpec,
    ConflictError,
    IndexSpec,
    Namespace,
    Query,
    Schema,
    TableSpec,
    apply_migrations,
)

REPLICAS = 12
ROWS_PER_REPLICA = 50
SCHEMA = Schema(
    tables=[
        TableSpec(
            slug="ticket",
            columns=[
                ColumnSpec(key="code", type_id="text", is_unique=True),
                ColumnSpec(key="priority", type_id="integer"),
                ColumnSpec(key="state", type_id="enum", config={"options": ["open", "closed"]}),
            ],
            indexes=[IndexSpec(column_keys=("state", "priority"))],
        )
    ]
)


async def replica(index: int, start_at: float) -> None:
    bind = engine(pool_size=3)
    await asyncio.sleep(max(0.0, start_at - time.time()))
    await apply_migrations(bind)
    async with session_of(bind) as s:
        for attempt in range(5):
            try:
                await Namespace(s, "shared").apply(SCHEMA)
                break
            except ConflictError:
                await s.rollback()
                await asyncio.sleep(0.05 * (attempt + 1))
        table = await Namespace(s, "shared").table("ticket")
        for n in range(ROWS_PER_REPLICA):
            await table.create({"code": f"R{index}-{n}", "priority": n % 5, "state": "open"})
        try:
            await table.create({"code": "SHARED-1", "priority": 0, "state": "open"})
            await s.commit()
            print("won-shared")
        except AlreadyExistsError:
            await s.rollback()
            for n in range(ROWS_PER_REPLICA):
                await table.create({"code": f"R{index}-{n}", "priority": n % 5, "state": "open"})
            await s.commit()
    await bind.dispose()


async def verify(report: Report) -> None:
    bind = engine()
    async with bind.connect() as conn:
        versions = (await conn.execute(sa.text("SELECT version FROM forktex_grid.schema_version"))).scalars().all()
        invalid = (
            await conn.execute(
                sa.text(
                    "SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                    "WHERE c.relname LIKE 'gx_%' AND NOT i.indisvalid"
                )
            )
        ).scalar_one()
        grid_indexes = (
            await conn.execute(sa.text("SELECT count(*) FROM pg_class WHERE relname LIKE 'gx_%'"))
        ).scalar_one()
    check(len(versions) == 1, f"schema_version rows: {versions}")
    check(invalid == 0, f"{invalid} invalid grid indexes left behind")
    check(grid_indexes == 2, f"expected 2 grid indexes (unique code, state+priority), found {grid_indexes}")
    async with session_of(bind) as s:
        table = await Namespace(s, "shared").table("ticket")
        total = await table.count()
        shared = await table.count(
            Query.model_validate({"where": {"column": "code", "op": "eq", "value": "SHARED-1"}}).where
        )
        distinct = len({r["code"] for r in (await table.query(Query.model_validate({"page": {"size": 1000}}))).items})
    check(shared == 1, f"SHARED-1 exists {shared} times")
    check(total == REPLICAS * ROWS_PER_REPLICA + 1, f"rows: {total}")
    check(distinct == total, "duplicate codes")
    await apply_migrations(bind)
    async with session_of(bind) as s:
        again = await Namespace(s, "shared").apply(SCHEMA)
    check(again.plan.changes == (), "a second boot planned changes")
    await bind.dispose()
    report.set("replicas", REPLICAS)
    report.set("rows", total)


def main() -> None:
    if len(sys.argv) == 3:
        asyncio.run(replica(int(sys.argv[1]), float(sys.argv[2])))
        return
    report = Report()
    start_at = time.time() + 3
    procs = [
        subprocess.Popen(
            [sys.executable, __file__, str(i), str(start_at)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        for i in range(REPLICAS)
    ]
    outputs = [p.communicate(timeout=300) for p in procs]
    failures = [err[-2000:] for p, (_, err) in zip(procs, outputs, strict=True) if p.returncode]
    check(not failures, "replica failed:\n" + "\n---\n".join(failures))
    report.set("won_shared", sum(out.count("won-shared") for out, _ in outputs))
    check(report.facts["won_shared"] == 1, f"{report.facts['won_shared']} replicas won the unique race")
    asyncio.run(verify(report))
    report.emit()


if __name__ == "__main__":
    main()
