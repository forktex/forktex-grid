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
import uuid
from decimal import Decimal
from typing import Any

from _consumer import Report, check, engine, rng, session_of

from forktex_grid import (
    AlreadyExistsError,
    ApplyOptions,
    BadRequestError,
    CursorPaging,
    IndexBuilds,
    Namespace,
    NotFoundError,
    Query,
    Schema,
    SortKey,
    apply_migrations,
)

STEPS = int(os.environ.get("BATTLE_STEPS", "2500"))
NAMESPACES = ("north", "south")
COUNTRIES = ["RO", "DE", "FR"]
INLINE = ApplyOptions(index_builds=IndexBuilds.inline)

SCHEMA = Schema.model_validate(
    {
        "tables": [
            {
                "slug": "client",
                "columns": [
                    {"key": "name", "typeId": "text", "isUnique": True, "isRequired": True},
                    {"key": "country", "typeId": "enum", "config": {"options": COUNTRIES}},
                    {"key": "vip", "typeId": "boolean"},
                ],
            },
            {"slug": "tag", "columns": [{"key": "label", "typeId": "text"}]},
            {
                "slug": "invoice",
                "columns": [
                    {"key": "number", "typeId": "text", "isUnique": True, "isRequired": True},
                    {"key": "total", "typeId": "decimal", "config": {"precision": 12, "scale": 2}},
                    {"key": "client", "typeId": "ref", "config": {"table": "client", "onDelete": "cascade"}},
                    {
                        "key": "tags",
                        "typeId": "ref",
                        "cardinality": "many",
                        "config": {"table": "tag", "onDelete": "set_null"},
                    },
                ],
                "indexes": [{"columnKeys": ["total"]}],
            },
            {
                "slug": "note",
                "columns": [
                    {"key": "invoice", "typeId": "ref", "config": {"table": "invoice", "onDelete": "restrict"}},
                    {"key": "text", "typeId": "text"},
                ],
            },
            {
                "slug": "memo",
                "extends": "public.account",
                "columns": [{"key": "score", "typeId": "integer"}, {"key": "flag", "typeId": "boolean"}],
            },
        ]
    }
)

Rows = dict[str, dict[uuid.UUID, dict[str, Any]]]


def without_empty(values: dict[str, object]) -> dict[str, object]:
    return {k: x for k, x in values.items() if x is not None and x != []}


class Model:
    def __init__(self) -> None:
        self.rows: dict[str, Rows] = {ns: {t.slug: {} for t in SCHEMA.tables} for ns in NAMESPACES}
        self.memo_refs: dict[str, dict[str, uuid.UUID]] = {ns: {} for ns in NAMESPACES}

    def copy(self) -> Model:
        twin = Model()
        twin.rows = {
            ns: {t: {i: dict(v) for i, v in rows.items()} for t, rows in tables.items()}
            for ns, tables in self.rows.items()
        }
        twin.memo_refs = {ns: dict(m) for ns, m in self.memo_refs.items()}
        return twin

    def unique_taken(self, ns: str, table: str, key: str, value: Any, besides: uuid.UUID | None = None) -> bool:
        return any(r.get(key) == value and i != besides for i, r in self.rows[ns][table].items())

    def archive(self, ns: str, table: str, row_id: uuid.UUID) -> bool:
        tables = self.rows[ns]
        doomed: list[tuple[str, uuid.UUID]] = []

        def plan(t: str, i: uuid.UUID) -> bool:
            if (t, i) in doomed:
                return True
            doomed.append((t, i))
            target = str(i)
            if t == "invoice":
                if any(n.get("invoice") == target for n in tables["note"].values()):
                    return False
            if t == "client":
                for inv_id, inv in tables["invoice"].items():
                    if inv.get("client") == target and not plan("invoice", inv_id):
                        return False
            return True

        if not plan(table, row_id):
            return False
        gone = {str(i) for _, i in doomed}
        for t, i in doomed:
            tables[t].pop(i, None)
        for inv in tables["invoice"].values():
            if inv.get("tags"):
                inv["tags"] = [x for x in inv["tags"] if x not in gone]
        return True


def holds(where: dict[str, Any], row: dict[str, Any], clients: dict[uuid.UUID, dict[str, Any]]) -> bool | None:
    if "and" in where:
        parts = [holds(w, row, clients) for w in where["and"]]
        return False if False in parts else None if None in parts else True
    if "or" in where:
        parts = [holds(w, row, clients) for w in where["or"]]
        return True if True in parts else None if None in parts else False
    if "not" in where:
        inner = holds(where["not"], row, clients)
        return None if inner is None else not inner
    if "link" in where:
        target = row.get("client")
        target_row = clients.get(uuid.UUID(target)) if target else None
        matched = target_row is not None and holds(where["where"], target_row, clients) is True
        return matched if where.get("match", "some") == "some" else not matched
    column, op, value = where["column"], where["op"], where["value"]
    stored = row.get(column)
    if op == "is_null":
        return (stored is None) == value
    if stored is None:
        return None
    if column == "total":
        stored, value = Decimal(stored), Decimal(value)
    return {"eq": stored == value, "ne": stored != value, "gte": stored >= value, "lt": stored < value}[op]


class Soak:
    def __init__(self, report: Report) -> None:
        self.r = rng("soak")
        self.model = Model()
        self.report = report
        self.serial = 0

    def name(self, prefix: str) -> str:
        self.serial += 1
        return f"{prefix}-{self.r.randint(0, self.serial)}"

    def random_where(self, depth: int = 0) -> dict[str, Any]:
        kind = self.r.random()
        if depth < 2 and kind < 0.25:
            return {self.r.choice(["and", "or"]): [self.random_where(depth + 1) for _ in range(self.r.randint(1, 3))]}
        if depth < 2 and kind < 0.32:
            return {"not": self.random_where(depth + 1)}
        if depth < 2 and kind < 0.45:
            inner = {"column": "country", "op": self.r.choice(["eq", "ne"]), "value": self.r.choice(COUNTRIES)}
            return {"link": "client", "match": self.r.choice(["some", "none"]), "where": inner}
        column = self.r.choice(["total", "number"])
        if column == "total":
            return (
                {
                    "column": "total",
                    "op": self.r.choice(["eq", "ne", "gte", "lt"]),
                    "value": f"{self.r.randint(0, 500)}.00",
                }
                if self.r.random() > 0.1
                else {"column": "total", "op": "is_null", "value": self.r.random() < 0.5}
            )
        return {"column": "number", "op": self.r.choice(["eq", "ne"]), "value": f"N-{self.r.randint(0, self.serial)}"}

    async def step(self, s: Any, ns: str) -> None:
        grid = Namespace(s, ns)
        model = self.model.rows[ns]
        roll = self.r.random()
        if roll < 0.12:
            name = self.name("C")
            values = {"name": name, "country": self.r.choice(COUNTRIES), "vip": self.r.random() < 0.3}
            try:
                row = await (await grid.table("client")).create(values)
                check(not self.model.unique_taken(ns, "client", "name", name), f"duplicate client name {name} accepted")
                model["client"][row.id] = dict(row.values)
                self.report.count("created")
            except AlreadyExistsError:
                check(self.model.unique_taken(ns, "client", "name", name), f"client {name} refused as duplicate")
                self.report.count("refused_unique")
        elif roll < 0.18:
            row = await (await grid.table("tag")).create({"label": self.name("T")})
            model["tag"][row.id] = dict(row.values)
        elif roll < 0.40:
            number = self.name("N")
            clients, tags = list(model["client"]), list(model["tag"])
            values = {
                "number": number,
                "total": None if self.r.random() < 0.1 else f"{self.r.randint(0, 500)}.{self.r.randint(0, 99):02d}",
                "client": str(self.r.choice(clients)) if clients and self.r.random() < 0.85 else None,
                "tags": [str(t) for t in self.r.sample(tags, k=min(len(tags), self.r.randint(0, 3)))],
            }
            if self.r.random() < 0.05:
                values["client"] = str(uuid.uuid7())
            try:
                row = await (await grid.table("invoice")).create(values)
                check(not self.model.unique_taken(ns, "invoice", "number", number), f"duplicate invoice {number}")
                check(
                    values["client"] is None or uuid.UUID(values["client"]) in model["client"],
                    "dangling client accepted",
                )
                model["invoice"][row.id] = dict(row.values)
            except AlreadyExistsError:
                check(self.model.unique_taken(ns, "invoice", "number", number), f"invoice {number} wrongly refused")
                self.report.count("refused_unique")
            except BadRequestError:
                check(
                    values["client"] is not None and uuid.UUID(values["client"]) not in model["client"],
                    "valid invoice refused",
                )
                self.report.count("refused_dangling")
        elif roll < 0.48 and model["invoice"]:
            target = self.r.choice(list(model["invoice"]))
            row = await (await grid.table("note")).create({"invoice": str(target), "text": "x"})
            model["note"][row.id] = dict(row.values)
        elif roll < 0.58 and model["invoice"]:
            target = self.r.choice(list(model["invoice"]))
            total = f"{self.r.randint(0, 500)}.00"
            row = await (await grid.table("invoice")).patch(target, values={"total": total})
            model["invoice"][target] = dict(row.values)
            check(Decimal(row["total"]) == Decimal(total), "patch lost the value")
        elif roll < 0.68:
            table = self.r.choice(["client", "invoice", "tag", "note"])
            if not model[table]:
                return
            target = self.r.choice(list(model[table]))
            twin = self.model.copy()
            allowed = twin.archive(ns, table, target)
            try:
                await (await grid.table(table)).archive(target)
                check(allowed, f"archive of {table} should have been refused")
                self.model = twin
                self.report.count("archived")
            except BadRequestError:
                check(not allowed, f"archive of {table} wrongly refused")
                self.report.count("refused_restrict")
        elif roll < 0.76:
            account = f"acct-{self.r.randint(0, 40)}"
            values = {"score": self.r.randint(-5, 5)} if self.r.random() < 0.5 else {"flag": self.r.random() < 0.5}
            row = await (await grid.table("memo")).upsert_by_external_ref(account, values=values)
            known = self.model.memo_refs[ns].get(account)
            check(known is None or known == row.id, "upsert created a second row for one host key")
            self.model.memo_refs[ns][account] = row.id
            merged = {**model["memo"].get(row.id, {}), **values}
            model["memo"][row.id] = merged
            check(
                row.values == {k: v for k, v in merged.items() if v is not None or k in row.values},
                f"upsert merged wrongly: {row.values} vs {merged}",
            )
        elif roll < 0.82:
            missing = uuid.uuid7()
            try:
                await (await grid.table("invoice")).get(missing)
                check(False, "a missing row was found")
            except NotFoundError:
                pass
        else:
            await self.verify_query(grid, ns)

    async def verify_query(self, grid: Namespace, ns: str) -> None:
        model = self.model.rows[ns]
        where = self.random_where()
        invoices = await grid.table("invoice")
        expected = {i for i, r in model["invoice"].items() if holds(where, r, model["client"]) is True}
        got: set[uuid.UUID] = set()
        after = None
        while True:
            page = await invoices.query(
                Query.model_validate(
                    {"where": where, "page": {"by": "cursor", "size": 1000, "after": after}, "count": True}
                )
            )
            got |= {r.id for r in page.items}
            if not page.has_more:
                break
            after = page.next_cursor
        check(got == expected, f"query {where}: {len(got)} rows, model says {len(expected)}")
        check(page.total == len(expected), "count disagrees")
        self.report.count("queries_checked")

    async def full_compare(self, s: Any) -> None:
        for ns in NAMESPACES:
            for table, rows in self.model.rows[ns].items():
                found = {}
                after = None
                while True:
                    page = await (await Namespace(s, ns).table(table)).query(
                        Query(
                            order_by=(SortKey(column="created_at", direction="desc"),),
                            page=CursorPaging(size=97, after=after),
                        )
                    )
                    found |= {r.id: dict(r.values) for r in page.items}
                    if not page.has_more:
                        break
                    after = page.next_cursor
                check(set(found) == set(rows), f"{ns}.{table}: grid holds {len(found)} rows, model {len(rows)}")
                for i, values in rows.items():
                    check(
                        without_empty(found[i]) == without_empty(values),
                        f"{ns}.{table} row differs: {found[i]} vs {values}",
                    )
        self.report.count("full_compares")


async def main() -> None:
    report = Report()
    bind = engine(pool_size=4)
    await apply_migrations(bind)
    async with session_of(bind) as s:
        for ns in NAMESPACES:
            await Namespace(s, ns).apply(SCHEMA, INLINE)
        await s.commit()
    soak = Soak(report)
    for step in range(STEPS):
        ns = soak.r.choice(NAMESPACES)
        async with session_of(bind) as s:
            if soak.r.random() < 0.05:
                before = soak.model.copy()
                try:
                    await soak.step(s, ns)
                finally:
                    await s.rollback()
                soak.model = before
                report.count("rolled_back")
            else:
                await soak.step(s, ns)
                await s.commit()
        if step % 500 == 499:
            async with session_of(bind) as s:
                await soak.full_compare(s)
    async with session_of(bind) as s:
        await soak.full_compare(s)
    report.set("steps", STEPS)
    report.emit()
    await bind.dispose()


if __name__ == "__main__":
    asyncio.run(main())
