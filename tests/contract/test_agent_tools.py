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

import inspect

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_grid import BadRequestError, FieldType, Namespace, NotFoundError, Table
from forktex_grid.agent import READ_TOOLS, TOOLS, run, tool_schemas

SCHEMA = {
    "tables": [
        {
            "slug": "finding",
            "columns": [
                {"key": "claim", "typeId": "text", "isRequired": True},
                {"key": "confidence", "typeId": "decimal"},
            ],
        },
        {"slug": "memory", "extends": "public.conversation", "columns": [{"key": "summary", "typeId": "text"}]},
    ]
}


def _public_coroutines(owner: type) -> dict[str, inspect.Signature]:
    return {
        name: inspect.signature(member)
        for name, member in vars(owner).items()
        if not name.startswith("_") and inspect.iscoroutinefunction(member)
    }


def test_every_public_method_is_a_tool_with_its_own_parameters() -> None:
    methods = _public_coroutines(Namespace) | _public_coroutines(Table)
    methods.pop("table")
    methods.pop("migrate_to")
    assert set(TOOLS) == set(methods) | {"field_types"}
    for name, signature in methods.items():
        params = [p for p in signature.parameters if p != "self"]
        fields = list(TOOLS[name].model_fields)
        assert fields == (["table", *params] if name in _public_coroutines(Table) else params), name


def test_the_tool_schemas_describe_the_schema_document_and_each_tool() -> None:
    schemas = tool_schemas()
    assert set(schemas) == set(TOOLS)
    assert {"TableSpec", "ColumnSpec", "IndexSpec"} <= set(schemas["apply"]["$defs"])
    assert all(schema.get("description") for schema in schemas.values())
    assert list(schemas["query"]["properties"]) == ["table", "query"]
    assert {"Query", "Comparison", "And", "Or", "Not", "Linked", "Embed"} <= set(schemas["query"]["$defs"])
    assert list(schemas["apply"]["properties"]) == ["desired", "options"]


def test_a_tool_refuses_unknown_and_missing_arguments() -> None:
    with pytest.raises(ValidationError):
        TOOLS["query"].model_validate({"table": "t", "query": {"limt": 5}})
    with pytest.raises(ValidationError):
        TOOLS["get"].model_validate({"table": "t"})


async def test_bad_arguments_are_a_grid_error_with_one_line_per_problem(session: AsyncSession) -> None:
    ns = Namespace(session, "agent-args")
    with pytest.raises(BadRequestError) as refused:
        await run(ns, "query", {"table": "t", "query": {"limt": 5}})
    assert str(refused.value) == "invalid arguments for 'query': query.limt: Extra inputs are not permitted"
    assert refused.value.details == {"problems": ["query.limt: Extra inputs are not permitted"]}
    with pytest.raises(BadRequestError, match=r"rowId: Field required") as missing:
        await run(ns, "get", {"table": "t"})
    assert "pydantic.dev" not in str(missing.value)


async def test_a_column_config_error_names_the_problem_without_links(session: AsyncSession) -> None:
    desired = {"tables": [{"slug": "t", "columns": [{"key": "s", "typeId": "enum", "config": {"values": ["a"]}}]}]}
    with pytest.raises(BadRequestError) as refused:
        await run(Namespace(session, "agent-config"), "apply", {"desired": desired})
    message = str(refused.value)
    assert "options: Field required" in message and "values: Extra inputs are not permitted" in message
    assert "pydantic.dev" not in message


async def test_a_refusal_from_a_spec_rule_reads_like_any_bad_argument(session: AsyncSession) -> None:
    ns = Namespace(session, "agent-spec-rules")
    for column, problem in (
        ({"key": "s", "typeId": "enum"}, "invalid enum config: options: Field required"),
        ({"key": "id", "typeId": "text"}, "column key 'id' is reserved"),
    ):
        with pytest.raises(BadRequestError) as refused:
            await run(ns, "apply", {"desired": {"tables": [{"slug": "t", "columns": [column]}]}})
        assert str(refused.value).startswith("invalid arguments for 'apply': ")
        assert problem in str(refused.value)
        assert problem in refused.value.details["problems"][0]


def test_a_batch_row_must_name_its_op_and_the_schema_says_so() -> None:
    defs = tool_schemas()["batch"]["$defs"]
    for kind in ("CreateRow", "UpsertRow", "PatchRow", "ArchiveRow"):
        assert defs[kind]["required"][0] == "op", kind
        assert "default" not in defs[kind]["properties"]["op"], kind
    with pytest.raises(ValidationError):
        TOOLS["batch"].model_validate({"batch": {"rows": [{"table": "t", "values": {}}]}})


def test_each_field_type_is_a_column_variant_with_its_own_config() -> None:
    defs = tool_schemas()["apply"]["$defs"]
    column = defs["ColumnSpec"]
    assert set(column["discriminator"]["mapping"]) == {str(t) for t in FieldType}
    enum = defs["ColumnSpec_enum"]
    assert enum["properties"]["typeId"] == {"const": "enum"}
    assert enum["properties"]["config"]["required"] == ["options"]
    assert "config" in enum["required"]
    ref = defs["ColumnSpec_ref"]["properties"]["config"]
    assert ref["required"] == ["table"] and ref["properties"]["onDelete"]["$ref"].startswith("#/$defs/ref_")
    assert "config" not in defs["ColumnSpec_text"]["required"]
    assert all("title" not in str(defs["ColumnSpec_text"]["properties"][k]) for k in ("isRequired", "key"))


def test_a_read_only_agent_gets_only_tools_that_cannot_write() -> None:
    assert set(tool_schemas(READ_TOOLS)) == READ_TOOLS
    assert not READ_TOOLS & {"apply", "batch", "create", "create_many", "patch", "archive", "rename_column"}
    assert READ_TOOLS <= set(TOOLS)


async def test_a_tool_outside_the_given_set_is_refused(session: AsyncSession) -> None:
    ns = Namespace(session, "agent-read-only")
    with pytest.raises(BadRequestError, match="tool 'apply' is not available here"):
        await run(ns, "apply", {"desired": SCHEMA}, only=READ_TOOLS)
    assert await run(ns, "describe", {}, only=READ_TOOLS) == {"result": {"tables": []}}


async def test_an_unknown_tool_is_refused(session: AsyncSession) -> None:
    with pytest.raises(BadRequestError, match="unknown tool"):
        await run(Namespace(session, "agent"), "drop_database", {})


async def test_field_types_lists_every_builtin(session: AsyncSession) -> None:
    types = (await run(Namespace(session, "agent"), "field_types", {}))["result"]
    assert set(FieldType) <= set(types)
    assert types["enum"]["config"]["required"] == ["options"]
    assert "eq" in types["text"]["capabilities"]["filterOps"]


async def test_a_model_works_a_namespace_through_the_tools(session: AsyncSession) -> None:
    ns = Namespace(session, "agent-run")

    applied = (await run(ns, "apply", {"desired": SCHEMA, "options": {"indexBuilds": "inline"}}))["result"]
    assert [c["op"] for c in applied["plan"]["changes"]] == ["create_table", "create_table"]
    assert applied["complete"] is True
    described = (await run(ns, "describe", {}))["result"]
    assert [t["slug"] for t in described["tables"]] == ["finding", "memory"]

    rows = [{"claim": "a", "confidence": 0.9}, {"claim": "b", "confidence": 0.2}]
    created = (await run(ns, "create_many", {"table": "finding", "values": rows}))["result"]
    first = created[0]
    assert first["values"] == {"claim": "a", "confidence": "0.9"}

    query = {"orderBy": [{"column": "confidence", "direction": "desc"}], "count": True}
    page = (await run(ns, "query", {"table": "finding", "query": query}))["result"]
    assert page["total"] == 2
    where = {"column": "confidence", "op": "gt", "value": 0.5}
    assert (await run(ns, "count", {"table": "finding", "where": where}))["result"] == 1
    assert [r["values"]["claim"] for r in page["items"]] == ["a", "b"] and page["hasMore"] is False

    patched = (await run(ns, "patch", {"table": "finding", "rowId": first["id"], "values": {"claim": "a2"}}))["result"]
    assert patched["values"]["claim"] == "a2"
    assert (await run(ns, "get", {"table": "finding", "rowId": first["id"]}))["result"]["values"]["claim"] == "a2"

    assert await run(ns, "archive", {"table": "finding", "rowId": first["id"]}) == {"result": None}
    with pytest.raises(NotFoundError):
        await run(ns, "get", {"table": "finding", "rowId": first["id"]})

    upsert = {"table": "memory", "externalRef": "conv-1", "values": {"summary": "x"}}
    one = (await run(ns, "upsert_by_external_ref", upsert))["result"]
    two = (await run(ns, "upsert_by_external_ref", {**upsert, "values": {"summary": "y"}}))["result"]
    assert one["id"] == two["id"] and two["externalRef"] == "conv-1"
    found = await run(ns, "get_by_external_ref", {"table": "memory", "externalRef": "conv-1"})
    assert found["result"]["values"] == {"summary": "y"}
    assert await run(ns, "get_by_external_ref", {"table": "memory", "externalRef": "conv-2"}) == {"result": None}


async def test_batch_writes_schema_and_rows_together(session: AsyncSession) -> None:
    ns = Namespace(session, "agent-batch")
    document = {"tables": [{"slug": "note", "columns": [{"key": "text", "typeId": "text"}]}]}
    rows = [{"op": "create", "table": "note", "values": {"text": "hello"}}]
    report = (await run(ns, "batch", {"batch": {"desired": document, "rows": rows}}))["result"]
    assert report["schemaReport"]["plan"]["changes"][0]["op"] == "create_table"
    assert report["results"][0]["row"]["values"] == {"text": "hello"}


async def test_counts_by_value_and_by_table(session: AsyncSession) -> None:
    ns = Namespace(session, "agent-counts")
    document = {
        "tables": [
            {"slug": "task", "columns": [{"key": "status", "typeId": "enum", "config": {"options": ["todo", "done"]}}]},
            {"slug": "empty", "columns": [{"key": "x", "typeId": "text"}]},
        ]
    }
    statuses = ["todo", "todo", "done", None]
    rows = [{"op": "create", "table": "task", "values": {"status": s}} for s in statuses]
    await run(ns, "batch", {"batch": {"desired": document, "rows": rows}})
    counted = (await run(ns, "count_by", {"table": "task", "column": "status"}))["result"]
    assert counted == {
        "groups": [{"value": "todo", "count": 2}, {"value": "done", "count": 1}, {"value": None, "count": 1}],
        "others": 0,
    }
    assert (await run(ns, "row_counts", {}))["result"] == {"empty": 0, "task": 4}


async def test_a_batch_dry_run_rehearses_its_rows_and_keeps_nothing(session: AsyncSession) -> None:
    ns = Namespace(session, "agent-rehearsal")
    document = {"tables": [{"slug": "note", "columns": [{"key": "n", "typeId": "integer"}]}]}
    rows = [{"op": "create", "table": "note", "values": {"n": 1}}]
    rehearsed = await run(ns, "batch", {"batch": {"desired": document, "rows": rows}, "options": {"dryRun": True}})
    assert rehearsed["result"]["dryRun"] is True
    assert rehearsed["result"]["results"][0]["row"]["values"] == {"n": 1}
    assert (await run(ns, "describe", {}))["result"] == {"tables": []}
    bad = [{"op": "create", "table": "note", "values": {"n": "x"}}]
    with pytest.raises(BadRequestError, match="column 'n'"):
        await run(ns, "batch", {"batch": {"desired": document, "rows": bad}, "options": {"dryRun": True}})


async def test_a_refused_call_changes_nothing(session: AsyncSession) -> None:
    ns = Namespace(session, "agent-refused")
    document = {"tables": [{"slug": "note", "columns": [{"key": "n", "typeId": "integer"}]}]}
    rows = [{"op": "create", "table": "note", "values": {"n": "not a number"}}]
    with pytest.raises(BadRequestError):
        await run(ns, "batch", {"batch": {"desired": document, "rows": rows}})
    assert (await run(ns, "describe", {}))["result"] == {"tables": []}
