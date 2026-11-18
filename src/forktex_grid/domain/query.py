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

import enum
from typing import Annotated, Any, Literal

from forktex.database.filters import FilterOp, SortDirection, SortKey
from forktex.types import BaseWireValueObject
from pydantic import ConfigDict, Discriminator, Field, Tag

SYSTEM_FIELDS = frozenset({"id", "external_ref", "created_at", "updated_at"})


class LinkMatch(enum.StrEnum):
    some = "some"
    every = "every"
    none = "none"


class _Node(BaseWireValueObject):
    model_config = ConfigDict(extra="forbid")

    def __and__(self, other: Where) -> And:
        return And.of(self, other)

    def __or__(self, other: Where) -> Or:
        return Or.of(self, other)

    def __invert__(self) -> Not:
        return Not.of(self)


class Comparison(_Node):
    column: str
    op: FilterOp
    value: Any = None


class And(_Node):
    all_of: tuple[Where, ...] = Field(alias="and", min_length=1)

    @classmethod
    def of(cls, *conditions: _Node) -> And:
        flat = [part for c in conditions for part in (c.all_of if isinstance(c, And) else (c,))]
        return cls.model_validate({"and": flat})


class Or(_Node):
    any_of: tuple[Where, ...] = Field(alias="or", min_length=1)

    @classmethod
    def of(cls, *conditions: _Node) -> Or:
        flat = [part for c in conditions for part in (c.any_of if isinstance(c, Or) else (c,))]
        return cls.model_validate({"or": flat})


class Not(_Node):
    negated: Where = Field(alias="not")

    @classmethod
    def of(cls, condition: _Node) -> Not:
        return cls.model_validate({"not": condition})


class Linked(_Node):
    link: str
    match: LinkMatch = LinkMatch.some
    where: Where


def _node_kind(data: object) -> str | None:
    if isinstance(data, _Node):
        return type(data).__name__
    if isinstance(data, dict):
        for key, kind in (("and", "And"), ("or", "Or"), ("not", "Not"), ("link", "Linked")):
            if key in data:
                return kind
        return "Comparison"
    return None


type Where = Annotated[
    Annotated[Comparison, Tag("Comparison")]
    | Annotated[And, Tag("And")]
    | Annotated[Or, Tag("Or")]
    | Annotated[Not, Tag("Not")]
    | Annotated[Linked, Tag("Linked")],
    Discriminator(_node_kind),
]


class Embed(BaseWireValueObject):
    model_config = ConfigDict(extra="forbid")

    link: str
    select: tuple[str, ...] | None = None


type SelectItem = str | Embed


class OffsetPaging(BaseWireValueObject):
    model_config = ConfigDict(extra="forbid")

    by: Literal["offset"] = "offset"
    size: int = 50
    offset: int = 0


class CursorPaging(BaseWireValueObject):
    model_config = ConfigDict(extra="forbid")

    by: Literal["cursor"] = "cursor"
    size: int = 50
    after: str | None = None


def _paging_kind(data: object) -> str:
    if isinstance(data, OffsetPaging | CursorPaging):
        return data.by
    if isinstance(data, dict):
        return str(data.get("by") or ("cursor" if "after" in data else "offset"))
    return "offset"


type Paging = Annotated[
    Annotated[OffsetPaging, Tag("offset")] | Annotated[CursorPaging, Tag("cursor")],
    Discriminator(_paging_kind),
]


class Query(BaseWireValueObject):
    model_config = ConfigDict(extra="forbid")

    where: Where | None = None
    order_by: tuple[SortKey, ...] = ()
    select: tuple[SelectItem, ...] | None = None
    page: Paging = Field(default_factory=OffsetPaging)
    count: bool = False

    @property
    def embeds(self) -> tuple[Embed, ...]:
        return tuple(item for item in self.select or () if isinstance(item, Embed))

    @property
    def columns(self) -> tuple[str, ...] | None:
        return None if self.select is None else tuple(item for item in self.select if isinstance(item, str))


class Column:
    __slots__ = ("key",)

    def __init__(self, key: str) -> None:
        self.key = key

    def _compare(self, op: FilterOp, value: object) -> Comparison:
        return Comparison(column=self.key, op=op, value=value)

    def __eq__(self, value: object) -> Comparison:
        return self._compare(FilterOp.eq, value)

    def __ne__(self, value: object) -> Comparison:
        return self._compare(FilterOp.ne, value)

    def __lt__(self, value: object) -> Comparison:
        return self._compare(FilterOp.lt, value)

    def __le__(self, value: object) -> Comparison:
        return self._compare(FilterOp.lte, value)

    def __gt__(self, value: object) -> Comparison:
        return self._compare(FilterOp.gt, value)

    def __ge__(self, value: object) -> Comparison:
        return self._compare(FilterOp.gte, value)

    def is_in(self, values: list[object]) -> Comparison:
        return self._compare(FilterOp.in_, list(values))

    def not_in(self, values: list[object]) -> Comparison:
        return self._compare(FilterOp.not_in, list(values))

    def between(self, low: object, high: object) -> Comparison:
        return self._compare(FilterOp.between, [low, high])

    def is_null(self, null: bool = True) -> Comparison:
        return self._compare(FilterOp.is_null, null)

    def contains(self, text: str) -> Comparison:
        return self._compare(FilterOp.contains, text)

    def icontains(self, text: str) -> Comparison:
        return self._compare(FilterOp.icontains, text)

    def startswith(self, text: str) -> Comparison:
        return self._compare(FilterOp.starts_with, text)

    def endswith(self, text: str) -> Comparison:
        return self._compare(FilterOp.ends_with, text)

    def fuzzy(self, text: str) -> Comparison:
        return self._compare(FilterOp.fuzzy, text)

    def asc(self) -> SortKey:
        return SortKey(column=self.key, direction=SortDirection.asc)

    def desc(self) -> SortKey:
        return SortKey(column=self.key, direction=SortDirection.desc)


class Link:
    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def where(self, condition: Where) -> Linked:
        return self.some(condition)

    def some(self, condition: Where) -> Linked:
        return Linked(link=self.name, match=LinkMatch.some, where=condition)

    def every(self, condition: Where) -> Linked:
        return Linked(link=self.name, match=LinkMatch.every, where=condition)

    def none(self, condition: Where) -> Linked:
        return Linked(link=self.name, match=LinkMatch.none, where=condition)

    def select(self, *columns: str) -> Embed:
        return Embed(link=self.name, select=columns or None)


def col(key: str) -> Column:
    return Column(key)


def link(name: str) -> Link:
    return Link(name)


__all__ = [
    "SYSTEM_FIELDS",
    "And",
    "Column",
    "Comparison",
    "CursorPaging",
    "Embed",
    "Link",
    "LinkMatch",
    "Linked",
    "Not",
    "OffsetPaging",
    "Or",
    "Paging",
    "Query",
    "SelectItem",
    "Where",
    "col",
    "link",
]
