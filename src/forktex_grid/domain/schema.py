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

from typing import Any

from forktex.database.identifiers import validate_identifier, validate_relation, validate_slug
from forktex.types import BaseWireValueObject, JsonObject, JsonValue
from pydantic import ConfigDict, Field, ValidationError, model_validator

from forktex_grid.domain.builtin_types import RefConfig
from forktex_grid.domain.enums import Cardinality, FieldType
from forktex_grid.domain.fieldtypes import FieldTypeHandler, IndexKind, get_field_type
from forktex_grid.domain.limits import MAX_LABEL, MAX_NAME, holds_nul, storable_text
from forktex_grid.domain.query import SYSTEM_FIELDS
from forktex_grid.domain.validation import summary
from forktex_grid.errors import BadRequestError


def _parse_config(handler: FieldTypeHandler[Any], key: object, raw: JsonObject) -> JsonObject:
    try:
        return handler.parse_config(raw).model_dump(mode="json", exclude_defaults=True)
    except ValidationError as exc:
        raise BadRequestError(f"column '{key}': invalid {handler.type_id} config: {summary(exc)}") from None
    except (ValueError, BadRequestError) as exc:
        raise BadRequestError(f"column '{key}': invalid {handler.type_id} config: {exc}") from None


class ColumnSpec(BaseWireValueObject):
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str = ""
    type_id: str
    cardinality: Cardinality = Cardinality.one
    is_required: bool = False
    is_unique: bool = False
    config: JsonObject = Field(default_factory=dict)
    default_value: JsonValue = None
    display_order: int = 0

    @model_validator(mode="before")
    @classmethod
    def _canonical(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        filled = {**data, "label": data.get("label") or data.get("key")}
        type_id = filled.get("type_id", filled.get("typeId"))
        if isinstance(type_id, str):
            filled["config"] = _parse_config(get_field_type(type_id), filled.get("key"), filled.get("config") or {})
            if holds_nul(filled["config"]):
                raise BadRequestError(f"column '{filled.get('key')}': its config may not contain a NUL character")
        return filled

    @model_validator(mode="after")
    def _validate(self) -> ColumnSpec:
        validate_identifier(self.key)
        storable_text(self.key, "a column key", max_length=MAX_NAME)
        storable_text(self.label, f"column '{self.key}' label", max_length=MAX_LABEL)
        if self.key in SYSTEM_FIELDS:
            raise BadRequestError(f"column key '{self.key}' is reserved: every row already has it")
        handler = get_field_type(self.type_id)
        config = handler.parse_config(self.config)
        if self.is_unique and (self.cardinality is Cardinality.many or not handler.capabilities.index_kinds):
            raise BadRequestError(f"column '{self.key}' cannot be unique: {self.type_id} ({self.cardinality})")
        if self.default_value is not None:
            try:
                values = self.default_value if self.cardinality is Cardinality.many else [self.default_value]
                if not isinstance(values, list):
                    raise ValueError("a many column's default is a list")
                for value in values:
                    handler.normalize(value, config=config)
            except ValueError as exc:
                raise BadRequestError(f"column '{self.key}': invalid default: {exc}") from None
        return self


class IndexSpec(BaseWireValueObject):
    model_config = ConfigDict(extra="forbid")

    column_keys: tuple[str, ...] = Field(min_length=1)
    kind: IndexKind = "btree"
    is_unique: bool = False

    @model_validator(mode="after")
    def _validate(self) -> IndexSpec:
        for key in self.column_keys:
            validate_identifier(key)
        if self.is_unique and self.kind != "btree":
            raise BadRequestError("a unique index is a btree")
        return self

    @property
    def signature(self) -> str:
        return f"{'unique ' if self.is_unique else ''}{self.kind}({','.join(self.column_keys)})"


class TableSpec(BaseWireValueObject):
    model_config = ConfigDict(extra="forbid")

    slug: str
    label: str = ""
    extends: str | None = None
    columns: tuple[ColumnSpec, ...] = ()
    indexes: tuple[IndexSpec, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _label_defaults_to_slug(cls, data: object) -> object:
        if isinstance(data, dict) and not data.get("label"):
            return {**data, "label": data.get("slug")}
        return data

    @model_validator(mode="after")
    def _validate(self) -> TableSpec:
        validate_slug(self.slug)
        storable_text(self.slug, "a table slug", max_length=MAX_NAME)
        storable_text(self.label, f"table '{self.slug}' label", max_length=MAX_LABEL)
        if self.extends is not None:
            validate_relation(self.extends)
            storable_text(self.extends, f"table '{self.slug}' extends", max_length=MAX_LABEL)
        by_key = {c.key: c for c in self.columns}
        if len(by_key) != len(self.columns):
            raise BadRequestError(f"table '{self.slug}' has duplicate column keys")
        if len({i.signature for i in self.indexes}) != len(self.indexes):
            raise BadRequestError(f"table '{self.slug}' has duplicate indexes")
        for index in self.indexes:
            for key in index.column_keys:
                column = by_key.get(key)
                if column is None:
                    raise BadRequestError(f"index {index.signature} on '{self.slug}': no column '{key}'")
                if column.cardinality is Cardinality.many:
                    raise BadRequestError(f"index {index.signature} on '{self.slug}': '{key}' holds a list")
                if index.kind not in get_field_type(column.type_id).capabilities.index_kinds:
                    raise BadRequestError(
                        f"index {index.signature} on '{self.slug}': {column.type_id} has no {index.kind} index"
                    )
        return self

    def column(self, key: str) -> ColumnSpec:
        for col in self.columns:
            if col.key == key:
                return col
        raise BadRequestError(f"table '{self.slug}' has no column '{key}'")


class Schema(BaseWireValueObject):
    model_config = ConfigDict(extra="forbid")

    tables: tuple[TableSpec, ...] = ()

    @model_validator(mode="after")
    def _unique_slugs(self) -> Schema:
        slugs = [t.slug for t in self.tables]
        if len(set(slugs)) != len(slugs):
            raise BadRequestError(f"duplicate tables: {sorted({s for s in slugs if slugs.count(s) > 1})}")
        return self

    @property
    def by_slug(self) -> dict[str, TableSpec]:
        return {t.slug: t for t in self.tables}

    def table(self, slug: str) -> TableSpec:
        try:
            return self.by_slug[slug]
        except KeyError:
            raise BadRequestError(f"schema has no table '{slug}'") from None

    def merged_with(self, patch: Schema, *, prune: bool) -> Schema:
        return patch if prune else Schema(tables=tuple((self.by_slug | patch.by_slug).values()))

    def check(self) -> Schema:
        slugs = self.by_slug
        for table in self.tables:
            for col in table.columns:
                if col.type_id == FieldType.ref:
                    target = RefConfig.model_validate(col.config).table
                    if target not in slugs:
                        raise BadRequestError(f"column '{table.slug}.{col.key}' refers to unknown table '{target}'")
        return self


__all__ = ["ColumnSpec", "IndexSpec", "Schema", "TableSpec"]
