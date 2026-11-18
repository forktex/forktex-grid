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
import random
import time
import uuid
from typing import Any

from _consumer import Report, check, engine, rng, session_of

from forktex_grid import (
    ApplyOptions,
    Cardinality,
    ColumnSpec,
    CursorPaging,
    IndexBuilds,
    Migration,
    Namespace,
    Query,
    Schema,
    TableSource,
    TableSpec,
    apply_migrations,
)

ROWS = int(os.environ.get("BATTLE_MIGRATE_ROWS", "1500"))
INLINE = ApplyOptions(index_builds=IndexBuilds.inline)
PLAIN = ["text", "integer", "decimal", "boolean", "date", "enum"]

type FactsByLabel = dict[str, dict[str, Any]]


def random_schema(r: random.Random) -> Schema:
    slugs = [f"t{i}" for i in range(r.randint(3, 5))]
    tables = []
    for slug in slugs:
        columns = [ColumnSpec(key="label", type_id="text", is_required=True, is_unique=True)]
        for c in range(r.randint(1, 4)):
            type_id = r.choice(PLAIN)
            config = {"options": ["a", "b", "c"]} if type_id == "enum" else {}
            columns.append(ColumnSpec(key=f"c{c}", type_id=type_id, config=config))
        for link in range(r.randint(1, 2)):
            many = r.random() < 0.4
            columns.append(
                ColumnSpec(
                    key=f"l{link}",
                    type_id="ref",
                    cardinality=Cardinality.many if many else Cardinality.one,
                    config={"table": r.choice(slugs), "onDelete": "set_null"},
                )
            )
        tables.append(TableSpec(slug=slug, columns=tuple(columns)))
    return Schema(tables=tuple(tables))


def plain_value(r: random.Random, type_id: str) -> Any:
    if r.random() < 0.2:
        return None
    return {
        "text": lambda: r.choice(["alpha", "beta", "gamma", "δέλτα", ""]),
        "integer": lambda: r.randint(-3, 3),
        "decimal": lambda: f"{r.randint(-999, 999) / 100:.2f}",
        "boolean": lambda: r.random() < 0.5,
        "date": lambda: f"2026-{r.randint(1, 12):02d}-{r.randint(1, 28):02d}",
        "enum": lambda: r.choice(["a", "b", "c"]),
    }[type_id]()


async def fill(ns: Namespace, schema: Schema, r: random.Random, report: Report) -> None:
    await ns.apply(schema, INLINE)
    ids: dict[str, list[str]] = {t.slug: [] for t in schema.tables}
    specs = {t.slug: t for t in schema.tables}
    for n in range(ROWS):
        spec = specs[r.choice(list(specs))]
        values: dict[str, Any] = {"label": f"{spec.slug}-{n}"}
        for column in spec.columns[1:]:
            if column.type_id != "ref":
                values[column.key] = plain_value(r, column.type_id)
                continue
            pool = ids[column.config["table"]]
            if not pool or r.random() < 0.25:
                continue
            values[column.key] = (
                r.sample(pool, k=min(len(pool), r.randint(0, 3))) if column.cardinality == "many" else r.choice(pool)
            )
        ref = f"ext-{n}" if r.random() < 0.3 else None
        row = await (await ns.table(spec.slug)).create(values, external_ref=ref)
        ids[spec.slug].append(str(row.id))
    for _ in range(ROWS // 10):
        spec = specs[r.choice(list(specs))]
        links = [c for c in spec.columns if c.type_id == "ref" and c.cardinality == "one"]
        if not links or not ids[spec.slug]:
            continue
        column = r.choice(links)
        target = ids[column.config["table"]]
        if target:
            row_id = uuid.UUID(r.choice(ids[spec.slug]))
            await (await ns.table(spec.slug)).patch(row_id, values={column.key: r.choice(target)})
            report.count("cycle_patches")
    for _ in range(ROWS // 20):
        slug = r.choice(list(specs))
        if ids[slug]:
            await (await ns.table(slug)).archive(uuid.UUID(ids[slug].pop(r.randrange(len(ids[slug])))))
            report.count("archived")


async def facts(ns: Namespace, rename: dict[str, str] | None = None) -> FactsByLabel:
    back = {new: old for old, new in (rename or {}).items()}
    labels: dict[str, str] = {}
    raw = []
    for spec in (await ns.describe()).tables:
        table = await ns.table(spec.slug)
        links = {c.key: c.cardinality == "many" for c in spec.columns if c.type_id == "ref"}
        label_key = next(c.key for c in spec.columns if c.key.endswith("label"))
        cursor = None
        while True:
            page = await table.query(Query(page=CursorPaging(size=500, after=cursor)))
            for row in page.items:
                labels[str(row.id)] = str(row.values[label_key])
                raw.append((row, links))
            if not page.next_cursor:
                break
            cursor = page.next_cursor
    out: FactsByLabel = {}
    for row, links in raw:
        values: dict[str, Any] = {}
        for key, value in row.values.items():
            if key in links and value is not None:
                value = sorted(labels[v] for v in value) if links[key] else labels[value]
            values[back.get(key, key)] = value
        values = {k: v for k, v in values.items() if v not in (None, [])}
        out[labels[str(row.id)]] = {
            "values": values,
            "external_ref": row.external_ref,
            "stamps": (row.created_at, row.updated_at),
        }
    return out


def same(before: FactsByLabel, after: FactsByLabel, what: str, *, merged_keys: set[str] = frozenset()) -> None:
    check(set(before) == set(after), f"{what}: rows differ: {sorted(set(before) ^ set(after))[:5]}")
    for label, fact in before.items():
        copy = after[label]
        values = {k: v for k, v in copy["values"].items() if k not in merged_keys}
        check(values == fact["values"], f"{what}: {label} holds {values} instead of {fact['values']}")
        check(copy["external_ref"] == fact["external_ref"], f"{what}: {label} lost its external ref")
        check(copy["stamps"] == fact["stamps"], f"{what}: {label} lost its timestamps")


async def migrate(bind: Any, source: str, target: str, migration: Migration, report: Report, step: str) -> None:
    async with session_of(bind) as s:
        started = time.perf_counter()
        rehearsed = await Namespace(s, source).migrate_to(Namespace(s, target), migration, ApplyOptions(dry_run=True))
        check((await Namespace(s, target).describe()).tables == (), f"{step}: a dry run left tables behind")
        real = await Namespace(s, source).migrate_to(Namespace(s, target), migration)
        await s.commit()
        report.timed(f"{step}_s", started)
    check(
        [t.model_dump() for t in rehearsed.tables] == [t.model_dump() for t in real.tables],
        f"{step}: the dry run reported something else than the migration did",
    )
    check(all(t.links_dropped == 0 for t in real.tables), f"{step}: links dropped: {real.tables}")
    report.set(f"{step}_rows", sum(t.rows for t in real.tables))


def renamed(schema: Schema) -> tuple[Schema, Migration, dict[str, str]]:
    keys: dict[str, str] = {}
    tables, sources = [], []
    for spec in schema.tables:
        columns = []
        for c in spec.columns:
            keys[c.key] = f"k_{c.key}"
            config = {**c.config, "table": f"x_{c.config['table']}"} if c.type_id == "ref" else c.config
            columns.append(c.model_copy(update={"key": f"k_{c.key}", "config": config}))
        tables.append(TableSpec(slug=f"x_{spec.slug}", columns=tuple(columns)))
        sources.append(
            TableSource(target=f"x_{spec.slug}", source=spec.slug, columns={f"k_{c.key}": c.key for c in spec.columns})
        )
    desired = Schema(tables=tuple(tables))
    return desired, Migration(desired=desired, tables=tuple(sources)), keys


def first_two_tables_merged(schema: Schema, keys: dict[str, str]) -> tuple[Migration, str, str]:
    first, second = schema.tables[0], schema.tables[1]
    into = "x_merged"

    def retarget(c: ColumnSpec) -> ColumnSpec:
        if c.type_id == "ref" and c.config["table"] in (first.slug, second.slug):
            return c.model_copy(update={"config": {**c.config, "table": into}})
        return c

    union: dict[str, ColumnSpec] = {}
    for spec in (first, second):
        for c in spec.columns:
            key = f"{spec.slug}__{c.key}" if c.key != "k_label" else c.key
            union[key] = retarget(c).model_copy(update={"key": key, "is_unique": c.key == "k_label"})
    union["origin"] = ColumnSpec(key="origin", type_id="text")
    others = [s.model_copy(update={"columns": tuple(retarget(c) for c in s.columns)}) for s in schema.tables[2:]]
    desired = Schema(tables=(TableSpec(slug=into, columns=tuple(union.values())), *others))
    sources = [
        TableSource(
            target=into,
            source=spec.slug,
            columns={(f"{spec.slug}__{c.key}" if c.key != "k_label" else c.key): c.key for c in spec.columns},
            constants={"origin": spec.slug},
        )
        for spec in (first, second)
    ] + [TableSource(target=s.slug, source=s.slug) for s in schema.tables[2:]]
    return Migration(desired=desired, tables=tuple(sources)), first.slug, second.slug


async def main() -> None:
    report = Report()
    r = rng("migrate")
    bind = engine()
    await apply_migrations(bind)
    schema = random_schema(r)
    async with session_of(bind) as s:
        await fill(Namespace(s, "kb-1"), schema, r, report)
        await s.commit()
    async with session_of(bind) as s:
        original = await facts(Namespace(s, "kb-1"))
    report.set("rows", len(original))

    as_is = Migration(desired=schema, tables=tuple(TableSource(target=t.slug, source=t.slug) for t in schema.tables))
    await migrate(bind, "kb-1", "kb-2", as_is, report, "as_is")
    async with session_of(bind) as s:
        same(original, await facts(Namespace(s, "kb-2")), "as is")
        for spec in schema.tables:
            for c in spec.columns:
                if c.type_id in PLAIN:
                    a = await (await Namespace(s, "kb-1").table(spec.slug)).count_by(c.key, limit=1000)
                    b = await (await Namespace(s, "kb-2").table(spec.slug)).count_by(c.key, limit=1000)
                    check(a == b, f"count_by {spec.slug}.{c.key} differs after migrating")
                    report.count("count_by_compared")

    renamed_schema, to_renamed, keys = renamed(schema)
    await migrate(bind, "kb-2", "kb-3", to_renamed, report, "renamed")
    async with session_of(bind) as s:
        same(original, await facts(Namespace(s, "kb-3"), keys), "renamed")

    to_merged, a, b = first_two_tables_merged(renamed_schema, keys)
    await migrate(bind, "kb-3", "kb-4", to_merged, report, "merged")
    async with session_of(bind) as s:
        after = await facts(Namespace(s, "kb-4"))
    unmerged = {}
    for label, fact in after.items():
        values = {}
        for key, value in fact["values"].items():
            if key == "origin":
                continue
            plain = key.split("__", 1)[1] if "__" in key else key
            values[plain[2:] if plain.startswith("k_") else plain] = value
        unmerged[label] = {**fact, "values": values}
    same(original, unmerged, "merged")
    origins = {fact["values"]["origin"] for fact in after.values() if "origin" in fact["values"]}
    check(origins <= {a, b}, f"merged rows carry an unknown origin: {origins}")
    report.emit()
    await bind.dispose()


if __name__ == "__main__":
    asyncio.run(main())
