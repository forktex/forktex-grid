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

import copy
import inspect
import typing
from collections.abc import Callable, Collection, Coroutine
from typing import Any

from forktex.types import BaseWireValueObject, JsonObject
from pydantic import ConfigDict, TypeAdapter, ValidationError, create_model

from forktex_grid.domain.fieldtypes import all_field_types
from forktex_grid.domain.validation import problems, summary
from forktex_grid.errors import BadRequestError
from forktex_grid.namespace import Namespace
from forktex_grid.table import Table

_DESCRIPTIONS: dict[str, str] = {
    "describe": "Return every table of the namespace, as a schema document.",
    "apply": (
        "Converge the namespace toward a schema document: create, change and drop tables, columns and "
        "indexes. Without `options.prune` the document is a patch; `options.dryRun` only reports the plan."
    ),
    "batch": (
        "Apply a batch document: an optional schema patch (`desired`), then `rows` written in order, all or nothing."
    ),
    "rename_column": "Rename a column of a table, carrying its values.",
    "create": "Create one row.",
    "create_many": "Create several rows; `externalRefs`, if given, pairs with `values` by position.",
    "get": "Read one row by its id.",
    "patch": "Set some columns of a row.",
    "archive": "Archive a row.",
    "get_by_external_ref": "Read the row linked to an external key, or null.",
    "get_many_by_external_ref": "Read the rows linked to any of the external keys, by key.",
    "upsert_by_external_ref": "Set columns on the row linked to an external key, creating it if there is none.",
    "query": (
        "Read a page of rows: `where` filters (and/or/not, and `link` to filter by a linked row), `orderBy` sorts, "
        "`select` names the columns and embeds linked rows, `page` is offset or cursor, `count` adds the total."
    ),
    "count": "Count the rows a `where` filter matches.",
    "count_by": (
        "Count the rows (matching `where`) per value of one column, most frequent first: "
        "`groups` of {value, count}, and `others` for what fell past `limit`. A list column counts each element."
    ),
    "row_counts": "Count the live rows of every table of the namespace, by table.",
    "field_types": "List the column types: the filters, sorts and indexes each supports, and its config.",
}


class _Input(BaseWireValueObject):
    model_config = ConfigDict(extra="forbid")


def _described(name: str) -> type[_Input]:
    config = ConfigDict(json_schema_extra={"description": _DESCRIPTIONS[name]})
    return type(f"_{name}", (_Input,), {"model_config": config})


class _Tool:
    __slots__ = ("model", "name", "owner", "params", "returns")

    def __init__(self, owner: type[Namespace] | type[Table], name: str) -> None:
        method: Callable[..., Coroutine[Any, Any, object]] = getattr(owner, name)
        hints = typing.get_type_hints(method)
        fields: dict[str, Any] = {"table": (str, ...)} if owner is Table else {}
        self.params: list[str] = []
        for param in list(inspect.signature(method).parameters.values())[1:]:
            default = ... if param.default is inspect.Parameter.empty else param.default
            fields[param.name] = (hints[param.name], default)
            self.params.append(param.name)
        self.model: type[_Input] = create_model(name, __base__=_described(name), **fields)
        self.owner = owner
        self.name = name
        self.returns: TypeAdapter[Any] = TypeAdapter(hints["return"])

    async def __call__(self, namespace: Namespace, given: dict[str, Any]) -> JsonObject:
        target = namespace if self.owner is Namespace else await namespace.table(given["table"])
        result = await getattr(target, self.name)(**{p: given[p] for p in self.params})
        return {"result": self.returns.dump_python(result, mode="json", by_alias=True)}


def _methods(owner: type[Namespace] | type[Table], skip: set[str]) -> list[_Tool]:
    return [
        _Tool(owner, name)
        for name, member in vars(owner).items()
        if not name.startswith("_") and inspect.iscoroutinefunction(member) and name not in skip
    ]


_TOOLS: dict[str, _Tool] = {t.name: t for t in _methods(Namespace, {"table", "migrate_to"}) + _methods(Table, set())}


_FieldTypes = _described("field_types")


TOOLS: dict[str, type[_Input]] = {name: tool.model for name, tool in _TOOLS.items()} | {"field_types": _FieldTypes}


READ_TOOLS: frozenset[str] = frozenset(
    {"describe", "row_counts", "get", "get_by_external_ref", "get_many_by_external_ref", "query", "count", "count_by"}
    | {"field_types"}
)


type JsonSchema = dict[str, Any]


def _untitled(node: object) -> object:
    if isinstance(node, dict):
        return {k: _untitled(v) for k, v in node.items() if not (k == "title" and isinstance(v, str))}
    if isinstance(node, list):
        return [_untitled(v) for v in node]
    return node


def _json_schema(model: type[BaseWireValueObject], ref_template: str = "#/$defs/{model}") -> JsonSchema:
    schema = _untitled(model.model_json_schema(by_alias=True, ref_template=ref_template))
    return schema if isinstance(schema, dict) else {}


def _typed_columns(schema: JsonSchema) -> JsonSchema:
    defs = schema.get("$defs")
    if not isinstance(defs, dict) or "ColumnSpec" not in defs:
        return schema
    base = defs["ColumnSpec"]
    if not isinstance(base, dict):
        return schema
    mapping: dict[str, str] = {}
    for type_id, handler in sorted(all_field_types().items()):
        name = f"ColumnSpec_{type_id}"
        config = _json_schema(handler.config_model, ref_template=f"#/$defs/{type_id}_{{model}}")
        for nested, definition in config.pop("$defs", {}).items():
            defs[f"{type_id}_{nested}"] = definition
        variant = copy.deepcopy(base)
        variant["title"] = f"{type_id} column"
        variant["properties"]["typeId"] = {"const": str(type_id)}
        variant["properties"]["config"] = config
        if config.get("required"):
            variant["required"] = [*variant.get("required", []), "config"]
        defs[name] = variant
        mapping[str(type_id)] = f"#/$defs/{name}"
    defs["ColumnSpec"] = {
        "oneOf": [{"$ref": ref} for ref in mapping.values()],
        "discriminator": {"propertyName": "typeId", "mapping": mapping},
    }
    return schema


def tool_schemas(only: Collection[str] | None = None) -> dict[str, JsonSchema]:
    names = TOOLS if only is None else [name for name in TOOLS if name in only]
    return {name: _typed_columns(_json_schema(TOOLS[name])) for name in names}


def _arguments(op: str, model: type[_Input], args: JsonObject) -> dict[str, Any]:
    try:
        return dict(model.model_validate(args))
    except ValidationError as exc:
        raise BadRequestError(
            f"invalid arguments for '{op}': {summary(exc)}", details={"problems": problems(exc)}
        ) from None
    except BadRequestError as exc:
        raise BadRequestError(
            f"invalid arguments for '{op}': {exc}", details={**(exc.details or {}), "problems": [str(exc)]}
        ) from None


async def run(namespace: Namespace, op: str, args: JsonObject, *, only: Collection[str] | None = None) -> JsonObject:
    if only is not None and op not in only:
        raise BadRequestError(f"tool '{op}' is not available here (available: {', '.join(sorted(only))})")
    model = TOOLS.get(op)
    if model is None:
        raise BadRequestError(f"unknown tool '{op}' (known: {', '.join(sorted(TOOLS))})")
    given = _arguments(op, model, args)
    if op == "field_types":
        return {
            "result": {
                type_id: {
                    "capabilities": handler.capabilities.model_dump(mode="json"),
                    "config": handler.config_model.model_json_schema(by_alias=True),
                }
                for type_id, handler in sorted(all_field_types().items())
            }
        }
    return await _TOOLS[op](namespace, given)


__all__ = ["READ_TOOLS", "TOOLS", "run", "tool_schemas"]
