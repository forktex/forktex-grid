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
import traceback
import uuid
from typing import Any

from _consumer import Report, check, engine, rng, session_of
from forktex.error import AppError
from pydantic import ValidationError

from forktex_grid import BadRequestError, Namespace, OffsetPaging, Query, apply_migrations
from forktex_grid.agent import READ_TOOLS, TOOLS, run

CALLS = int(os.environ.get("BATTLE_CALLS", "4000"))
AGENTS = 6
TYPES = ["text", "integer", "decimal", "boolean", "date", "datetime", "uuid", "enum", "json", "ref", "nope", ""]
KEYS = [
    "name",
    "amount",
    "when",
    "ok",
    "kind",
    "data",
    "link",
    "tags",
    "id",
    "created_at",
    "Bad Key",
    "x" * 70,
    "émoji🙂",
]
SLUGS = ["item", "owner", "event", "missing", "Bad-Slug", "a" * 80]
OPS = [
    "eq",
    "ne",
    "lt",
    "lte",
    "gt",
    "gte",
    "between",
    "in",
    "not_in",
    "contains",
    "icontains",
    "startswith",
    "endswith",
    "is_null",
    "fuzzy",
    "explode",
]


def scalar(r: random.Random) -> Any:
    return r.choice(
        [
            None,
            True,
            False,
            0,
            -1,
            2**63,
            3.5,
            float("1e308"),
            "",
            "x",
            "0042",
            "2026-11-13",
            "2026-13-45",
            "2026-11-13T10:00:00Z",
            str(uuid.uuid7()),
            "not-a-uuid",
            "a" * 5000,
            "\x00",
            "🙂",
            [1, 2],
            {"k": "v"},
            "12.345678901",
        ]
    )


def column(r: random.Random) -> dict[str, Any]:
    type_id = r.choice(TYPES)
    config: dict[str, Any] = {}
    if type_id == "enum":
        config = r.choice([{"options": ["a", "b"]}, {"options": []}, {}])
    elif type_id == "ref":
        config = {"table": r.choice(SLUGS), "onDelete": r.choice(["restrict", "cascade", "set_null", "explode"])}
    elif type_id == "decimal" and r.random() < 0.5:
        config = {"precision": r.randint(-1, 20), "scale": r.randint(-1, 20)}
    elif type_id == "text" and r.random() < 0.3:
        config = {"maxLength": r.randint(0, 10)}
    return {
        "key": r.choice(KEYS),
        "typeId": type_id,
        "config": config,
        "cardinality": r.choice(["one", "one", "many", "several"]),
        "isUnique": r.random() < 0.2,
        "isRequired": r.random() < 0.2,
        **({"defaultValue": scalar(r)} if r.random() < 0.1 else {}),
    }


def schema(r: random.Random) -> dict[str, Any]:
    return {
        "tables": [
            {
                "slug": r.choice(SLUGS),
                "columns": [column(r) for _ in range(r.randint(0, 5))],
                **(
                    {"indexes": [{"columnKeys": [r.choice(KEYS)], "kind": r.choice(["btree", "trgm", "hash"])}]}
                    if r.random() < 0.3
                    else {}
                ),
            }
            for _ in range(r.randint(1, 3))
        ]
    }


def where(r: random.Random, depth: int = 0) -> Any:
    roll = r.random()
    if depth < 3 and roll < 0.2:
        return {r.choice(["and", "or"]): [where(r, depth + 1) for _ in range(r.randint(0, 3))]}
    if depth < 3 and roll < 0.28:
        return {"not": where(r, depth + 1)}
    if depth < 3 and roll < 0.38:
        return {
            "link": r.choice(KEYS),
            "match": r.choice(["some", "every", "none", "all"]),
            "where": where(r, depth + 1),
        }
    if roll < 0.42:
        return scalar(r)
    return {"column": r.choice(KEYS), "op": r.choice(OPS), "value": scalar(r)}


def args_for(tool: str, r: random.Random, known_ids: list[str]) -> dict[str, Any]:
    table = r.choice(SLUGS)
    row_id = r.choice(known_ids) if known_ids and r.random() < 0.7 else scalar(r)
    values = {r.choice(KEYS): scalar(r) for _ in range(r.randint(0, 4))}
    match tool:
        case "apply":
            return {
                "desired": schema(r),
                "options": {
                    "indexBuilds": "inline",
                    "prune": r.random() < 0.1,
                    "allowDestructive": r.random() < 0.3,
                    "dryRun": r.random() < 0.2,
                },
            }
        case "batch":
            rows = [
                {
                    "op": r.choice(["create", "upsert", "patch", "archive", "explode"]),
                    "table": r.choice(SLUGS),
                    "values": values,
                    "externalRef": scalar(r),
                    "rowId": row_id,
                }
                for _ in range(r.randint(0, 3))
            ]
            return {
                "batch": {"desired": schema(r) if r.random() < 0.5 else None, "rows": rows},
                **({"options": {"dryRun": True}} if r.random() < 0.2 else {}),
            }
        case "describe" | "field_types":
            return {}
        case "rename_column":
            return {"table": table, "key": r.choice(KEYS), "newKey": r.choice(KEYS)}
        case "create":
            return {"table": table, "values": values, **({"externalRef": scalar(r)} if r.random() < 0.3 else {})}
        case "create_many":
            return {"table": table, "values": [values for _ in range(r.randint(0, 3))]}
        case "get" | "archive":
            return {"table": table, "rowId": row_id}
        case "patch":
            return {"table": table, "rowId": row_id, "values": values}
        case "get_by_external_ref":
            return {"table": table, "externalRef": scalar(r)}
        case "get_many_by_external_ref":
            return {"table": table, "externalRefs": [scalar(r) for _ in range(r.randint(0, 3))]}
        case "upsert_by_external_ref":
            return {"table": table, "externalRef": scalar(r), "values": values}
        case "query":
            query: dict[str, Any] = {"where": where(r)} if r.random() < 0.8 else {}
            if r.random() < 0.4:
                query["orderBy"] = [{"column": r.choice(KEYS), "direction": r.choice(["asc", "desc", "up"])}]
            if r.random() < 0.4:
                query["select"] = [r.choice(KEYS), {"link": r.choice(KEYS), "select": [r.choice(KEYS)]}]
            if r.random() < 0.3:
                query["page"] = r.choice(
                    [
                        {"size": r.randint(-1, 2000)},
                        {"by": "cursor", "after": r.choice([None, "garbage", "W10="])},
                        {"offset": r.randint(-5, 200000)},
                    ]
                )
            query["count"] = r.random() < 0.5
            return {"table": table, "query": query}
        case "count":
            return {"table": table, "where": where(r)}
        case "count_by":
            return {
                "table": table,
                "column": r.choice(KEYS),
                **({"where": where(r)} if r.random() < 0.5 else {}),
                **({"limit": r.randint(-1, 1200)} if r.random() < 0.3 else {}),
            }
        case "row_counts":
            return {}
    return {}


async def agent(n: int, bind: Any, report: Report) -> None:
    r = rng(f"agent{n}")
    namespace = f"agent-{n}"
    known_ids: list[str] = []
    tools = sorted(TOOLS)
    weights = [
        6 if t in ("apply", "create", "create_many", "query", "batch", "upsert_by_external_ref") else 2 for t in tools
    ]
    for _ in range(CALLS // AGENTS):
        tool = r.choices(tools, weights)[0]
        args = args_for(tool, r, known_ids)
        async with session_of(bind) as s:
            try:
                result = await run(Namespace(s, namespace), tool, args)
                await s.commit()
                report.count("ok")
                payload = result["result"]
                items = (
                    payload
                    if isinstance(payload, list)
                    else payload.get("items", [payload])
                    if isinstance(payload, dict)
                    else []
                )
                known_ids += [i["id"] for i in items if isinstance(i, dict) and "id" in i][:5]
            except AppError as refused:
                await s.rollback()
                report.count(type(refused).__name__)
            except Exception:
                await s.rollback()
                check(False, f"{traceback.format_exc(limit=-6)}\nESCAPED from tool {tool} with args {args!r:.800}")


async def integrity(bind: Any, report: Report) -> None:
    rows = tables = 0
    async with session_of(bind) as s:
        for n in range(AGENTS):
            ns = Namespace(s, f"agent-{n}")
            for spec in (await ns.describe()).tables:
                table = await ns.table(spec.slug)
                model = table.model()
                tables += 1
                for row in (await table.query(Query(page=OffsetPaging(size=1000)))).items:
                    try:
                        model.model_validate(row.values)
                    except ValidationError as exc:
                        check(False, f"stored row of {spec.slug} fails its own model: {row.values!r:.300} — {exc}")
                    check(spec.extends is None or row.external_ref is not None, "an extension row without its host key")
                    rows += 1
    report.set("tables_checked", tables)
    report.set("rows_checked", rows)


async def held(ns: Namespace) -> tuple[object, ...]:
    return (await ns.describe()).model_dump(), await ns.row_counts()


async def reader(n: int, bind: Any, report: Report) -> None:
    r = rng(f"reader{n}")
    tools = sorted(TOOLS)
    async with session_of(bind) as s:
        ns = Namespace(s, f"agent-{n}")
        before = await held(ns)
        known_ids = [
            str(row.id)
            for t in (await ns.describe()).tables
            for row in (await (await ns.table(t.slug)).query(Query(page=OffsetPaging(size=5)))).items
        ]
        for _ in range(CALLS // AGENTS // 4):
            tool = r.choice(tools)
            args = args_for(tool, r, known_ids)
            try:
                await run(ns, tool, args, only=READ_TOOLS)
                check(tool in READ_TOOLS, f"read-only agent ran {tool}")
                report.count("read_only_answered")
            except BadRequestError as refused:
                await s.rollback()
                report.count("read_only_refused" if tool not in READ_TOOLS else "read_only_bad_args")
                check(
                    tool in READ_TOOLS or "not available here" in str(refused),
                    f"{tool} refused for another reason: {refused}",
                )
            except AppError:
                await s.rollback()
                check(tool in READ_TOOLS, f"read-only agent reached {tool}")
                report.count("read_only_bad_args")
        await s.rollback()
        check(await held(ns) == before, f"a read-only agent changed namespace agent-{n}")


async def main() -> None:
    report = Report()
    bind = engine(pool_size=AGENTS * 2 + 2)
    await apply_migrations(bind)
    await asyncio.gather(*(agent(n, bind, report) for n in range(AGENTS)))
    await integrity(bind, report)
    await asyncio.gather(*(reader(n, bind, report) for n in range(AGENTS)))
    report.emit()
    await bind.dispose()


if __name__ == "__main__":
    asyncio.run(main())
