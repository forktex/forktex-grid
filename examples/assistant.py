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

"""A personal assistant whose memory is grid: an interactive loop over a live LLM.

The assistant keeps what you tell it in *knowledge spaces*, one grid namespace
each. It designs its own tables as it goes (``apply``), writes and patches rows,
answers questions with queries, describes its current configuration, and can
migrate a whole space into a new shape — all through ``forktex_grid.agent``,
plus five space tools defined here around ``Namespace.row_counts`` and
``Namespace.migrate_to`` (which is never handed to a model directly: it names a
second namespace, so this program asks you before running it).

    OPENAI_API_KEY=… DATABASE_URL=postgresql+asyncpg://… python examples/assistant.py

Needs ``openai`` installed beside grid. ``OPENAI_MODEL`` picks the model
(default ``gpt-5.5``). Type ``/help`` at the prompt for the local commands.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from typing import Any

from forktex.database import close_engine, get_engine, init_engine, session
from forktex.error import AppError
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_grid import (
    ApplyOptions,
    BadRequestError,
    ColumnSpec,
    CursorPaging,
    Migration,
    Namespace,
    NotFoundError,
    Query,
    Schema,
    TableSpec,
    apply_migrations,
    col,
)
from forktex_grid.agent import READ_TOOLS, run, tool_schemas

MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.5")
CONFIG_NAMESPACE = "assistant"
SPACE_NAMESPACE_PREFIX = "assistant/"
MAX_TOOL_ROUNDS = 25

DIM, BOLD, RED, GREEN, CYAN, RESET = "\033[2m", "\033[1m", "\033[31m", "\033[32m", "\033[36m", "\033[0m"

REGISTRY_SCHEMA = Schema(
    tables=[
        TableSpec(
            slug="space",
            label="Knowledge spaces",
            columns=[
                ColumnSpec(key="name", type_id="text", is_unique=True, is_required=True),
                ColumnSpec(key="purpose", type_id="text"),
                ColumnSpec(key="is_active", type_id="boolean", default_value=False),
                ColumnSpec(key="migrated_from", type_id="text"),
            ],
        )
    ]
)

SYSTEM = """You are a personal assistant with a durable memory you design yourself.

Your memory is organised in knowledge spaces. Each space is a set of typed tables
(text, integer, decimal, boolean, date, datetime, enum, json, uuid, ref = a link to
a row of another table). You shape them with `apply` (a schema patch: new tables,
new columns, indexes) and fill them with `create`, `create_many`, `patch`,
`upsert_by_external_ref`, `archive`, or several at once with `batch`. You answer with
`query`, `count`, and `count_by` for "how many per ...". Every grid tool takes an
optional `space`; without it, the active space is used.

How to work:
- Before storing something, look at the active space's schema (given below). Reuse
  a table when the thing fits; add a column when a fact has no place; create a
  table only for a genuinely new kind of thing. Prefer typed columns over json.
- Link related things with `ref` columns instead of repeating names.
- When the user corrects something, patch the row; never create a duplicate.
- Answer questions from the data, not from the conversation. Never count rows
  yourself from a page: use `count` or `count_by`.
- Destructive schema changes (`options.prune` or `options.allowDestructive`) and
  real migrations ask the user for confirmation themselves; propose them, then call.
- What `apply` cannot change in place (a column's type or cardinality, merging
  tables, a cleaner shape overall) is a `migrate_space` into a new space: design
  the new schema, say which old table fills each new one and from which columns,
  run it with `dryRun: true` first, show the user what would not be carried, then
  run it for real when they agree.
- Keep answers short. Say what you stored or changed in one line.
{mode}
Now is {now} (the user's local time): write every datetime with this offset.
Active space: `{space}` — {purpose}
Its current schema (row counts in brackets):
{schema}
"""

READ_ONLY_NOTE = "\nYou are in read-only mode: answer from the data; you cannot change it.\n"


def _ns(s: AsyncSession, space: str) -> Namespace:
    return Namespace(s, SPACE_NAMESPACE_PREFIX + space)


async def _registry_rows() -> list[dict[str, Any]]:
    async with session() as s:
        page = await (await Namespace(s, CONFIG_NAMESPACE).table("space")).query(Query(page=CursorPaging(size=200)))
        return [{"id": str(r.id), **r.values} for r in page.items]


async def _active() -> tuple[str, str]:
    for row in await _registry_rows():
        if row.get("is_active"):
            return row["name"], row.get("purpose") or ""
    raise RuntimeError("no active space")


async def _set_active(name: str) -> None:
    async with session() as s:
        spaces = await Namespace(s, CONFIG_NAMESPACE).table("space")
        target = await spaces.query(Query(where=col("name") == name))
        if not target.items:
            raise NotFoundError(f"no space '{name}' (see list_spaces)")
        for row in (await spaces.query(Query(where=col("is_active").is_in([True])))).items:
            await spaces.patch(row.id, values={"is_active": False})
        await spaces.patch(target.items[0].id, values={"is_active": True})
        await s.commit()


async def _register(name: str, purpose: str, migrated_from: str | None = None) -> None:
    async with session() as s:
        spaces = await Namespace(s, CONFIG_NAMESPACE).table("space")
        if (await spaces.query(Query(where=col("name") == name))).items:
            raise BadRequestError(f"space '{name}' already exists")
        await spaces.create({"name": name, "purpose": purpose, "migrated_from": migrated_from})
        await s.commit()


async def bootstrap() -> None:
    async with session() as s:
        await Namespace(s, CONFIG_NAMESPACE).apply(REGISTRY_SCHEMA)
    if not await _registry_rows():
        await _register("personal", "everything about my life and work, until a better shape is needed")
        await _set_active("personal")


async def overview(space: str) -> dict[str, Any]:
    async with session() as s:
        ns = _ns(s, space)
        schema = await ns.describe()
        counts = await ns.row_counts()
    return {
        "space": space,
        "tables": [
            {"rows": counts.get(t.slug, 0), **t.model_dump(mode="json", by_alias=True, exclude_defaults=True)}
            for t in schema.tables
        ],
    }


def _schema_text(view: dict[str, Any]) -> str:
    if not view["tables"]:
        return "  (empty: no tables yet)"
    lines = []
    for t in view["tables"]:
        cols = ", ".join(
            f"{c['key']}:{c['typeId']}"
            + (f"->{c['config']['table']}" if c["typeId"] == "ref" else "")
            + ("[]" if c.get("cardinality") == "many" else "")
            + ("!" if c.get("isRequired") else "")
            + ("*" if c.get("isUnique") else "")
            for c in t.get("columns", [])
        )
        lines.append(f"  {t['slug']} [{t['rows']}]: {cols}")
    return "\n".join(lines) + "\n  (! required, * unique, [] list)"


async def migrate_space(args: dict[str, Any]) -> dict[str, Any]:
    source = args.get("source") or (await _active())[0]
    target = args["target"]
    dry_run = bool(args.get("dryRun", False))
    if target in {r["name"] for r in await _registry_rows()}:
        raise BadRequestError(f"space '{target}' already exists; migrate into a new name")
    migration = Migration.model_validate(args["migration"])
    if not dry_run and not confirm(f"Migrate '{source}' into a new space '{target}'?"):
        return {"refused": "the user declined the migration"}
    async with session() as s:
        report = await _ns(s, source).migrate_to(_ns(s, target), migration, ApplyOptions(dry_run=dry_run))
        await s.commit()
    result: dict[str, Any] = report.model_dump(mode="json", by_alias=True, exclude={"schema_report"})
    if dry_run:
        return result
    await _register(target, args.get("purpose", ""), migrated_from=source)
    if args.get("switch", True):
        await _set_active(target)
        result["active"] = target
    return result


SPACE_PARAM = {"type": "string", "description": "Knowledge space; the active one when omitted."}


def _migration_schema_with_typed_columns() -> dict[str, Any]:
    schema = Migration.model_json_schema(by_alias=True)
    typed = tool_schemas(["apply"])["apply"]["$defs"]
    return {**{k: v for k, v in schema.items() if k != "$defs"}, "$defs": {**schema.get("$defs", {}), **typed}}


def own_tools() -> dict[str, dict[str, Any]]:
    migration = _migration_schema_with_typed_columns()
    defs = migration.pop("$defs")
    return {
        "list_spaces": {
            "description": "List every knowledge space: name, purpose, which is active, where it was migrated from.",
            "parameters": {"type": "object", "properties": {}},
        },
        "space_overview": {
            "description": "A space's full configuration: every table with its columns, indexes and row count.",
            "parameters": {"type": "object", "properties": {"space": SPACE_PARAM}},
        },
        "create_space": {
            "description": "Create a new, empty knowledge space (does not switch to it).",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "purpose": {"type": "string"}},
                "required": ["name", "purpose"],
            },
        },
        "switch_space": {
            "description": "Make another space the active one.",
            "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        },
        "migrate_space": {
            "description": (
                "Copy a space into a NEW space with a new schema, in one transaction (grid's migrate_to): "
                "`migration.desired` is the new schema; each `migration.tables` entry fills a new table from an "
                "old one (`columns`: new key -> old key; several entries may fill one table, merging them; "
                "`constants` fill a column for every copied row). Links are re-pointed, external refs and "
                "timestamps kept. The report lists what was not carried. `dryRun` rehearses and keeps nothing. "
                "The source space is left untouched."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Name of the new space."},
                    "purpose": {"type": "string"},
                    "source": SPACE_PARAM,
                    "migration": migration,
                    "dryRun": {"type": "boolean"},
                    "switch": {"type": "boolean", "description": "Make the new space active (default true)."},
                },
                "required": ["target", "migration"],
                "$defs": defs,
            },
        },
    }


def openai_tools(read_only: bool) -> list[dict[str, Any]]:
    tools = []
    for name, schema in tool_schemas(READ_TOOLS if read_only else None).items():
        params = dict(schema)
        description = params.pop("description", name)
        params["properties"] = {**params.get("properties", {}), "space": SPACE_PARAM}
        tools.append({"type": "function", "function": {"name": name, "description": description, "parameters": params}})
    for name, spec in own_tools().items():
        if read_only and name in {"create_space", "migrate_space"}:
            continue
        tools.append({"type": "function", "function": {"name": name, **spec}})
    return tools


def confirm(question: str) -> bool:
    answer = input(f"{BOLD}{question} [y/N] {RESET}").strip().lower()
    return answer in {"y", "yes"}


async def call(name: str, args: dict[str, Any], read_only: bool) -> object:
    if name == "list_spaces":
        return await _registry_rows()
    if name == "space_overview":
        return await overview(args.get("space") or (await _active())[0])
    if name == "switch_space":
        await _set_active(args["name"])
        return {"active": args["name"]}
    if not read_only and name == "create_space":
        await _register(args["name"], args["purpose"])
        return {"created": args["name"]}
    if not read_only and name == "migrate_space":
        return await migrate_space(args)
    space = args.pop("space", None) or (await _active())[0]
    opts = args.get("options") or {}
    destructive = name == "apply" and (opts.get("prune") or opts.get("allowDestructive")) and not opts.get("dryRun")
    if destructive and not confirm(f"Allow a destructive schema change in '{space}'?"):
        return {"refused": "the user declined the destructive change; try options.dryRun to show the plan"}
    async with session() as s:
        result = await run(_ns(s, space), name, args, only=READ_TOOLS if read_only else None)
        await s.commit()
    return result["result"]


def _short(value: object, limit: int = 240) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def system_message(read_only: bool) -> dict[str, str]:
    space, purpose = await _active()
    view = await overview(space)
    now = datetime.now().astimezone().isoformat(timespec="minutes")
    mode = READ_ONLY_NOTE if read_only else ""
    content = SYSTEM.format(mode=mode, now=now, space=space, purpose=purpose, schema=_schema_text(view))
    return {"role": "system", "content": content}


HELP = """/spaces            list knowledge spaces
/use <space>       switch the active space
/schema [space]    show a space's tables and row counts
/readonly          toggle read-only mode (the model gets grid's READ_TOOLS only)
/raw               toggle printing full tool results
/reset             forget the conversation (the memory stays)
/model <name>      change the model
/quit              leave"""


async def run_command_or_quit(text: str, space: str, state: dict[str, Any], history: list[dict[str, Any]]) -> bool:
    cmd, _, arg = text.partition(" ")
    match cmd:
        case "/quit":
            return False
        case "/spaces":
            for r in await _registry_rows():
                mark = "*" if r.get("is_active") else " "
                origin = f" (from {r['migrated_from']})" if r.get("migrated_from") else ""
                print(f" {mark} {r['name']}{origin}: {r.get('purpose') or ''}")
        case "/use":
            await _set_active(arg.strip())
        case "/schema":
            print(_schema_text(await overview(arg.strip() or space)))
        case "/readonly":
            state["read_only"] = not state["read_only"]
            print(f"read-only {'on' if state['read_only'] else 'off'}")
        case "/raw":
            state["raw"] = not state["raw"]
            print(f"raw results {'on' if state['raw'] else 'off'}")
        case "/reset":
            history.clear()
            print("conversation forgotten")
        case "/model":
            state["model"] = arg.strip() or state["model"]
            print(f"model {state['model']}")
        case _:
            print(HELP)
    return True


async def turn(client: AsyncOpenAI, history: list[dict[str, Any]], state: dict[str, Any]) -> None:
    tools = openai_tools(state["read_only"])
    for _ in range(MAX_TOOL_ROUNDS):
        response = await client.chat.completions.create(
            model=state["model"], messages=[await system_message(state["read_only"]), *history], tools=tools
        )
        message = response.choices[0].message
        history.append(message.model_dump(exclude_none=True))
        if not message.tool_calls:
            print(f"{GREEN}assistant>{RESET} {message.content}")
            return
        for tc in message.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
                print(f"  {DIM}→ {tc.function.name} {_short(args)}{RESET}")
                result = await call(tc.function.name, args, state["read_only"])
                content = json.dumps(result, ensure_ascii=False, default=str)
                print(f"  {DIM}← {_short(result, 2000 if state['raw'] else 160)}{RESET}")
            except (AppError, KeyError, json.JSONDecodeError) as exc:
                content = json.dumps({"error": type(exc).__name__, "message": str(exc)})
                print(f"  {RED}← {type(exc).__name__}: {_short(str(exc), 300)}{RESET}")
            history.append({"role": "tool", "tool_call_id": tc.id, "content": content})
    print(f"{RED}stopped after {MAX_TOOL_ROUNDS} tool rounds{RESET}")


async def main() -> None:
    logging.basicConfig(filename=os.environ.get("ASSISTANT_LOG", "assistant.log"), level=logging.INFO)
    await init_engine(os.environ["DATABASE_URL"])
    await apply_migrations(get_engine())
    await bootstrap()
    client = AsyncOpenAI()
    history: list[dict[str, Any]] = []
    state: dict[str, Any] = {"raw": False, "read_only": False, "model": MODEL}
    print(f"{BOLD}grid assistant{RESET} · model {MODEL} · /help for commands")
    try:
        while True:
            space, _ = await _active()
            try:
                text = input(f"\n{CYAN}[{space}{' · read-only' if state['read_only'] else ''}]{RESET} you> ").strip()
            except EOFError:
                break
            if not text:
                continue
            if text.startswith("/"):
                try:
                    if not await run_command_or_quit(text, space, state, history):
                        break
                except AppError as exc:
                    print(f"{RED}{exc}{RESET}")
                continue
            history.append({"role": "user", "content": text})
            await turn(client, history, state)
    finally:
        await close_engine()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
