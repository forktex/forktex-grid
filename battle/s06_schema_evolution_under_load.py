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
from collections import Counter
from typing import Any

import sqlalchemy as sa
from _consumer import Report, check, engine, rng, session_of
from forktex.error import AppError

from forktex_grid import (
    ApplyOptions,
    ColumnSpec,
    ConflictError,
    IndexSpec,
    Namespace,
    OffsetPaging,
    Query,
    Schema,
    TableSpec,
    apply_migrations,
    col,
)

SECONDS = float(os.environ.get("BATTLE_SECONDS", "40"))
WRITERS = 6
READERS = 3
KINDS = ["a", "b", "c", "d"]
ALLOW = ApplyOptions(allow_destructive=True)


class Evolving:
    def __init__(self) -> None:
        self.r = rng("evolve")
        self.kind_options = ["a", "b", "c"]
        self.kind_required = False
        self.code_unique = True
        self.extra: list[str] = []
        self.indexed = False
        self.renames = 0

    def schema(self) -> Schema:
        columns = [
            ColumnSpec(key="code", type_id="text", is_unique=self.code_unique),
            ColumnSpec(
                key="kind", type_id="enum", config={"options": self.kind_options}, is_required=self.kind_required
            ),
            ColumnSpec(key="qty", type_id="integer"),
            *(ColumnSpec(key=k, type_id="integer", default_value=0) for k in self.extra),
        ]
        indexes = [IndexSpec(column_keys=("kind", "qty"))] if self.indexed else []
        return Schema(tables=[TableSpec(slug="stock", columns=columns, indexes=indexes)])

    def mutate(self) -> str:
        roll = self.r.random()
        if roll < 0.2:
            self.kind_options = sorted(set(self.r.sample(KINDS, k=self.r.randint(1, 4))) | {"a"})
            return "options"
        if roll < 0.35:
            self.kind_required = not self.kind_required
            return "required"
        if roll < 0.5:
            self.code_unique = not self.code_unique
            return "unique"
        if roll < 0.65:
            self.indexed = not self.indexed
            return "index"
        if roll < 0.8 and len(self.extra) < 4:
            self.extra.append(f"x{self.r.randint(0, 999)}")
            self.extra = sorted(set(self.extra))
            return "add"
        if self.extra:
            self.extra.pop(self.r.randrange(len(self.extra)))
            return "drop"
        return "noop"


async def main() -> None:
    report = Report()
    bind = engine(pool_size=WRITERS + READERS + 6)
    await apply_migrations(bind)
    shape = Evolving()
    async with session_of(bind) as s:
        await Namespace(s, "evo").apply(shape.schema())
    deadline = time.monotonic() + SECONDS
    outcomes: Counter[str] = Counter()

    async def writer(n: int) -> None:
        r = rng(f"w{n}")
        while time.monotonic() < deadline:
            async with session_of(bind) as s:
                try:
                    stock = await Namespace(s, "evo").table("stock")
                    values: dict[str, Any] = {"code": f"S{r.randint(0, 3000)}", "qty": r.randint(0, 50)}
                    if r.random() < 0.9:
                        values["kind"] = r.choice(KINDS)
                    if r.random() < 0.7:
                        await stock.create(values)
                    else:
                        page = await stock.query(Query(page=OffsetPaging(size=5, offset=r.randint(0, 50))))
                        if page.items:
                            await stock.patch(page.items[0].id, values={"qty": r.randint(0, 50)})
                    await s.commit()
                    outcomes["write_ok"] += 1
                except ConflictError:
                    await s.rollback()
                    outcomes["write_conflict"] += 1
                except AppError as exc:
                    await s.rollback()
                    outcomes[f"write_{type(exc).__name__}"] += 1

    async def reader(n: int) -> None:
        r = rng(f"r{n}")
        while time.monotonic() < deadline:
            async with session_of(bind) as s:
                try:
                    stock = await Namespace(s, "evo").table("stock")
                    await stock.query(
                        Query(where=col("qty") >= r.randint(0, 50), count=True, page=OffsetPaging(size=50))
                    )
                    outcomes["read_ok"] += 1
                except AppError as exc:
                    outcomes[f"read_{type(exc).__name__}"] += 1

    async def evolver() -> None:
        while time.monotonic() < deadline:
            change = shape.mutate()
            async with session_of(bind) as s:
                try:
                    applied = await Namespace(s, "evo").apply(shape.schema(), ALLOW)
                    outcomes[f"apply_{change}"] += 1
                    if not applied.complete:
                        outcomes["apply_incomplete"] += 1
                        failures = {i.failure for i in applied.indexes if not i.ok}
                        check(failures <= {"duplicate_values"}, f"index build failed for another reason: {failures}")
                except AppError as exc:
                    await s.rollback()
                    outcomes[f"apply_refused_{change}"] += 1
                    async with session_of(bind) as fresh:
                        actual = (await Namespace(fresh, "evo").describe()).table("stock")
                    kind = actual.column("kind")
                    shape.kind_options = list(kind.config["options"])
                    shape.kind_required = kind.is_required
                    shape.code_unique = actual.column("code").is_unique
                    shape.extra = sorted(c.key for c in actual.columns if c.key.startswith("x"))
                    shape.indexed = bool(actual.indexes)
                    del exc
            await asyncio.sleep(0.2)

    await asyncio.wait_for(
        asyncio.gather(*(writer(n) for n in range(WRITERS)), *(reader(n) for n in range(READERS)), evolver()),
        SECONDS + 120,
    )

    async with session_of(bind) as s:
        ns = Namespace(s, "evo")
        final = await ns.apply(shape.schema(), ALLOW)
        stock = await ns.table("stock")
        model = stock.model()
        rows = 0
        after = None
        codes: Counter[object] = Counter()
        from forktex_grid import CursorPaging

        while True:
            page = await stock.query(Query(order_by=(col("id").asc(),), page=CursorPaging(size=1000, after=after)))
            for row in page.items:
                model.model_validate(row.values)
                codes[row.values.get("code")] += 1
                rows += 1
            if not page.has_more:
                break
            after = page.next_cursor
        spec = stock.spec
        if spec.column("code").is_unique and final.complete:
            duplicated = [c for c, n in codes.items() if c is not None and n > 1]
            check(not duplicated, f"unique code holds duplicates: {duplicated[:5]}")
    async with bind.connect() as conn:
        invalid = (
            await conn.execute(
                sa.text(
                    "SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                    "WHERE c.relname LIKE 'gx_%' AND NOT i.indisvalid"
                )
            )
        ).scalar_one()
    check(invalid == 0, f"{invalid} invalid grid indexes remain")
    report.set("rows", rows)
    report.set("final_complete", final.complete)
    report.set("outcomes", dict(sorted(outcomes.items())))
    report.emit()
    await bind.dispose()


if __name__ == "__main__":
    asyncio.run(main())
