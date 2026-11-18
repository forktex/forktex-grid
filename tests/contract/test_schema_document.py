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

import pytest
from pydantic import ValidationError

from forktex_grid import BadRequestError, ColumnSpec, IndexSpec, Schema, TableSpec
from forktex_grid.domain.diff import ChangeOp, diff

TEXT = ColumnSpec(key="name", type_id="text")


def _refused(match: str) -> pytest.RaisesExc[BadRequestError | ValidationError]:
    return pytest.raises((BadRequestError, ValidationError), match=match)


def test_labels_default_to_the_key_and_the_slug() -> None:
    table = TableSpec(slug="client", columns=[TEXT])
    assert table.label == "client"
    assert table.columns[0].label == "name"
    assert ColumnSpec(key="name", label="Name", type_id="text").label == "Name"


@pytest.mark.parametrize(
    ("model", "data"),
    [
        (ColumnSpec, {"key": "a", "type_id": "text", "colour": "red"}),
        (TableSpec, {"slug": "t", "owner": "me"}),
        (IndexSpec, {"column_keys": ["a"], "using": "gin"}),
        (Schema, {"tables": [], "relations": []}),
    ],
)
def test_unknown_keys_are_refused(model: type, data: dict) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(data)


def test_an_unregistered_type_is_refused() -> None:
    with _refused("unknown field type"):
        ColumnSpec(key="a", type_id="money")


@pytest.mark.parametrize(
    ("type_id", "config", "match"),
    [
        ("enum", {}, "config"),
        ("enum", {"options": []}, "config"),
        ("ref", {}, "config"),
        ("ref", {"table": "not a slug"}, "slug"),
        ("decimal", {"precision": 4, "scale": 6}, "config"),
        ("text", {"max_length": 0}, "config"),
        ("integer", {"anything": 1}, "config"),
    ],
)
def test_a_type_config_is_checked(type_id: str, config: dict, match: str) -> None:
    with _refused(match):
        ColumnSpec(key="a", type_id=type_id, config=config)


@pytest.mark.parametrize(
    "column",
    [
        {"key": "tags", "type_id": "text", "cardinality": "many", "is_unique": True},
        {"key": "blob", "type_id": "json", "is_unique": True},
        {"key": "flag", "type_id": "boolean", "is_unique": True},
    ],
)
def test_unique_needs_a_single_indexable_value(column: dict) -> None:
    with _refused("cannot be unique"):
        ColumnSpec.model_validate(column)


def test_a_default_is_checked_against_the_type() -> None:
    with _refused("invalid default"):
        ColumnSpec(key="n", type_id="integer", default_value="many")
    with _refused("invalid default"):
        ColumnSpec(key="t", type_id="enum", cardinality="many", config={"options": ["a"]}, default_value="a")
    assert ColumnSpec(key="t", type_id="enum", cardinality="many", config={"options": ["a"]}, default_value=["a"])


@pytest.mark.parametrize(
    ("columns", "index", "match"),
    [
        ([TEXT], IndexSpec(column_keys=("missing",)), "no column"),
        ([ColumnSpec(key="tags", type_id="text", cardinality="many")], IndexSpec(column_keys=("tags",)), "list"),
        ([ColumnSpec(key="n", type_id="integer")], IndexSpec(column_keys=("n",), kind="trgm"), "no trgm index"),
        ([ColumnSpec(key="j", type_id="json")], IndexSpec(column_keys=("j",)), "no btree index"),
    ],
)
def test_an_index_is_checked_against_its_columns(columns: list, index: IndexSpec, match: str) -> None:
    with _refused(match):
        TableSpec(slug="t", columns=columns, indexes=[index])


def test_a_unique_index_is_a_btree() -> None:
    with _refused("btree"):
        IndexSpec(column_keys=("a",), kind="trgm", is_unique=True)


def test_duplicates_are_refused() -> None:
    with _refused("duplicate column"):
        TableSpec(slug="t", columns=[TEXT, TEXT])
    with _refused("duplicate indexes"):
        TableSpec(slug="t", columns=[TEXT], indexes=[IndexSpec(column_keys=("name",))] * 2)
    with _refused("duplicate tables"):
        Schema(tables=[TableSpec(slug="t"), TableSpec(slug="t")])


@pytest.mark.parametrize("extends", ["public.engagement", "engagement"])
def test_extends_names_a_relation(extends: str) -> None:
    assert TableSpec(slug="t", extends=extends).extends == extends


@pytest.mark.parametrize("extends", ["public.engagement; drop table x", "a.b.c", ""])
def test_extends_refuses_anything_but_a_relation(extends: str) -> None:
    with pytest.raises((BadRequestError, ValidationError)):
        TableSpec(slug="t", extends=extends)


@pytest.mark.parametrize("slug", ["a b", "a;drop", "x" * 129, ""])
def test_a_slug_is_an_identifier(slug: str) -> None:
    with pytest.raises((BadRequestError, ValidationError)):
        TableSpec(slug=slug)


def _ref(target: str) -> ColumnSpec:
    return ColumnSpec(key="client", type_id="ref", config={"table": target})


def test_check_refuses_a_ref_to_a_table_outside_the_schema() -> None:
    schema = Schema(tables=[TableSpec(slug="invoice", columns=[_ref("client")])])
    with _refused("unknown table 'client'"):
        schema.check()
    whole = Schema(tables=[*schema.tables, TableSpec(slug="client", columns=[TEXT])])
    assert whole.check() is whole


def test_the_json_document_round_trips() -> None:
    schema = Schema(
        tables=[
            TableSpec(
                slug="invoice",
                extends="public.invoice",
                columns=[
                    ColumnSpec(key="number", type_id="text", is_unique=True, display_order=1),
                    ColumnSpec(key="total", type_id="decimal", config={"precision": 10, "scale": 2}),
                    _ref("client"),
                ],
                indexes=[IndexSpec(column_keys=("total",))],
            ),
            TableSpec(slug="client", columns=[TEXT]),
        ]
    )
    document = schema.model_dump(mode="json")
    assert "namespace" not in document
    assert Schema.model_validate(document) == schema


def test_merging_patches_or_replaces() -> None:
    a, b = TableSpec(slug="a"), TableSpec(slug="b")
    b2 = TableSpec(slug="b", label="B")
    have = Schema(tables=[a, b])
    patch = Schema(tables=[b2])
    assert have.merged_with(patch, prune=False).by_slug == {"a": a, "b": b2}
    assert have.merged_with(patch, prune=True) == patch


def test_table_lookup() -> None:
    schema = Schema(tables=[TableSpec(slug="a", columns=[TEXT])])
    assert schema.table("a").column("name") == TEXT
    with _refused("no table"):
        schema.table("b")
    with _refused("no column"):
        schema.table("a").column("other")


def test_an_identical_schema_has_no_changes() -> None:
    schema = Schema(tables=[TableSpec(slug="a", columns=[TEXT], indexes=[IndexSpec(column_keys=("name",))])])
    assert diff(schema, schema).changes == ()


def test_creates_come_first_and_drops_last() -> None:
    old = Schema(tables=[TableSpec(slug="gone", columns=[TEXT]), TableSpec(slug="kept", columns=[TEXT])])
    new = Schema(
        tables=[
            TableSpec(slug="kept", columns=[ColumnSpec(key="other", type_id="text")]),
            TableSpec(slug="fresh", columns=[TEXT], indexes=[IndexSpec(column_keys=("name",))]),
        ]
    )
    ops = [c.op for c in diff(old, new).changes]
    assert ops == [
        ChangeOp.create_table,
        ChangeOp.add_column,
        ChangeOp.create_index,
        ChangeOp.drop_column,
        ChangeOp.drop_table,
    ]


def test_targets_name_what_changes() -> None:
    old = Schema(tables=[TableSpec(slug="t", columns=[TEXT])])
    new = Schema(
        tables=[
            TableSpec(
                slug="t",
                label="T",
                columns=[TEXT, ColumnSpec(key="n", type_id="integer")],
                indexes=[IndexSpec(column_keys=("n",))],
            )
        ]
    )
    assert [c.target for c in diff(old, new).changes] == ["table:t", "column:t.n", "index:t.btree(n)"]


@pytest.mark.parametrize(
    ("change", "destructive"),
    [
        ({"label": "Name"}, False),
        ({"display_order": 3}, False),
        ({"is_required": True}, True),
        ({"is_unique": True}, True),
        ({"type_id": "integer", "config": {}}, True),
        ({"cardinality": "many"}, True),
        ({"config": {"max_length": 10}}, True),
    ],
)
def test_altering_a_column_is_destructive_when_it_tightens(change: dict, destructive: bool) -> None:
    old = Schema(tables=[TableSpec(slug="t", columns=[TEXT])])
    changed = ColumnSpec.model_validate({**TEXT.model_dump(by_alias=False), **change})
    new = Schema(tables=[TableSpec(slug="t", columns=[changed])])
    (only,) = diff(old, new).changes
    assert only.op is ChangeOp.alter_column
    assert only.destructive is destructive


def test_loosening_a_column_is_not_destructive() -> None:
    strict = ColumnSpec(key="name", type_id="text", is_required=True, is_unique=True)
    old = Schema(tables=[TableSpec(slug="t", columns=[strict])])
    new = Schema(tables=[TableSpec(slug="t", columns=[TEXT])])
    assert diff(old, new).destructive == ()


def test_a_required_column_without_a_default_is_destructive_to_add() -> None:
    old = Schema(tables=[TableSpec(slug="t")])
    required = ColumnSpec(key="a", type_id="text", is_required=True)
    with_default = ColumnSpec(key="b", type_id="text", is_required=True, default_value="x")
    changes = diff(old, Schema(tables=[TableSpec(slug="t", columns=[required, with_default])])).changes
    assert {c.key: c.destructive for c in changes} == {"a": True, "b": False}


def test_dropping_data_is_destructive_and_dropping_an_index_is_not() -> None:
    old = Schema(
        tables=[
            TableSpec(slug="t", columns=[TEXT], indexes=[IndexSpec(column_keys=("name",))]),
            TableSpec(slug="u"),
        ]
    )
    new = Schema(tables=[TableSpec(slug="t", columns=[TEXT])])
    assert {c.target: c.destructive for c in diff(old, new).changes} == {
        "index:t.btree(name)": False,
        "table:u": True,
    }


def test_changing_what_a_table_extends_is_destructive() -> None:
    old = Schema(tables=[TableSpec(slug="t", extends="public.a")])
    relabelled = diff(old, Schema(tables=[TableSpec(slug="t", label="T", extends="public.a")])).changes
    rebound = diff(old, Schema(tables=[TableSpec(slug="t", extends="public.b")])).changes
    assert [(c.op, c.destructive) for c in relabelled] == [(ChangeOp.alter_table, False)]
    assert [(c.op, c.destructive) for c in rebound] == [(ChangeOp.alter_table, True)]
