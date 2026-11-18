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
import time
import uuid

import sqlalchemy as sa
from _consumer import Report, check, engine, rng, session_of

from forktex_grid import ColumnSpec, IndexSpec, Namespace, OffsetPaging, Query, Schema, TableSpec, apply_migrations, col

ACCOUNTS = int(os.environ.get("BATTLE_ACCOUNTS", "20000"))
WORKERS = 16
UPSERTS_PER_WORKER = int(os.environ.get("BATTLE_UPSERTS", "150"))
HOT_KEYS = 400
SEGMENTS = ["gold", "silver", "bronze"]

FIELDS = TableSpec(
    slug="account_fields",
    label="Account",
    extends="public.account",
    columns=[
        ColumnSpec(key="segment", type_id="enum", config={"options": SEGMENTS}),
        ColumnSpec(key="owner", type_id="text"),
        ColumnSpec(key="score", type_id="integer"),
    ],
    indexes=[IndexSpec(column_keys=("segment",))],
)


async def host_table(bind: sa.ext.asyncio.AsyncEngine) -> list[uuid.UUID]:
    ids = [uuid.uuid7() for _ in range(ACCOUNTS)]
    async with bind.begin() as conn:
        await conn.execute(sa.text("CREATE TABLE public.account (id uuid PRIMARY KEY, name text NOT NULL)"))
        for chunk in range(0, ACCOUNTS, 5000):
            await conn.execute(
                sa.text("INSERT INTO public.account (id, name) VALUES (:id, :name)"),
                [{"id": i, "name": f"Account {n:05d}"} for n, i in enumerate(ids[chunk : chunk + 5000], start=chunk)],
            )
    return ids


async def main() -> None:
    report = Report()
    bind = engine(pool_size=WORKERS + 4)
    await apply_migrations(bind)
    accounts = await host_table(bind)
    async with session_of(bind) as s:
        await Namespace(s, "tenant").apply(Schema(tables=[FIELDS]))

    written: dict[str, dict[str, set[object]]] = {}
    hot = accounts[:HOT_KEYS]

    async def worker(n: int) -> None:
        r = rng(f"worker{n}")
        for _ in range(UPSERTS_PER_WORKER):
            key = str(r.choice(hot))
            values: dict[str, object] = {}
            if r.random() < 0.6:
                values["segment"] = r.choice(SEGMENTS)
            if r.random() < 0.5:
                values["owner"] = f"user-{r.randint(1, 9)}"
            if r.random() < 0.5 or not values:
                values["score"] = r.randint(0, 100)
            for field, value in values.items():
                written.setdefault(key, {}).setdefault(field, set()).add(value)
            async with session_of(bind) as s:
                fields = await Namespace(s, "tenant").table("account_fields")
                await fields.upsert_by_external_ref(key, values=values)
                await s.commit()
            report.count("upserts")

    started = time.perf_counter()
    await asyncio.gather(*(worker(n) for n in range(WORKERS)))
    report.timed("racing_upserts_s", started)

    async with session_of(bind) as s:
        fields = await Namespace(s, "tenant").table("account_fields")
        everything = (await fields.query(Query(page=OffsetPaging(size=1000)))).items
        check(len(everything) == len(written), f"{len(everything)} grid rows for {len(written)} touched accounts")
        by_key = {r.external_ref: r for r in everything}
        check(len(by_key) == len(everything), "two grid rows extend one account")
        for key, fields_written in written.items():
            row = by_key[key]
            for field, values in fields_written.items():
                check(row.values.get(field) in values, f"{key}.{field} = {row.values.get(field)} was never written")

        started = time.perf_counter()
        async with bind.connect() as conn:
            page = (
                (await conn.execute(sa.text("SELECT id FROM public.account ORDER BY name LIMIT 100"))).scalars().all()
            )
        screen = await fields.get_many_by_external_ref(page)
        report.timed("list_screen_100_s", started)
        check(set(screen) == {str(i) for i in page} & set(by_key), "list screen disagrees with the full scan")

        started = time.perf_counter()
        gold = await fields.query(Query(where=col("segment") == "gold", select=(), page=OffsetPaging(size=1000)))
        gold_keys = [uuid.UUID(r.external_ref) for r in gold.items if r.external_ref]
        async with bind.connect() as conn:
            names = (
                (
                    await conn.execute(
                        sa.text("SELECT name FROM public.account WHERE id = ANY(:ids)"), {"ids": gold_keys}
                    )
                )
                .scalars()
                .all()
            )
        report.timed("filter_then_host_join_s", started)
        check(
            len(names) == len(gold_keys) == sum(1 for r in everything if r.values.get("segment") == "gold"), "gold join"
        )
        report.set("gold_accounts", len(gold_keys))
        report.set("accounts_extended", len(everything))
    report.emit()
    await bind.dispose()


if __name__ == "__main__":
    asyncio.run(main())
