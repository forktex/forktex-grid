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

import uuid
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from forktex.database.filters import LIKE_OPS, FilterOp, SortDirection, SortKey, compile_comparison
from forktex.database.filters import Comparison as SubstrateComparison
from forktex.database.pagination import encode_cursor, keyset_predicate
from forktex.types import JsonValue
from sqlalchemy.orm import aliased

from forktex_grid.domain.builtin_types import RefConfig
from forktex_grid.domain.enums import Cardinality, FieldType
from forktex_grid.domain.limits import storable_text
from forktex_grid.domain.query import And, Comparison, Linked, LinkMatch, Not, Or, Where
from forktex_grid.domain.resolved import ResolvedColumn, ResolvedTable
from forktex_grid.errors import BadRequestError
from forktex_grid.persist.models import GridRow
from forktex_grid.read.fields import FieldSource, QueryField, RowsEntity

type SortOrder = list[tuple[QueryField, bool]]

TEXT_PATTERN_OPS = LIKE_OPS | {FilterOp.fuzzy}

MAX_LINK_DEPTH = 1


def ref_target(table: ResolvedTable, link: str) -> tuple[ResolvedColumn, str]:
    column = table.column(link)
    if column.spec.type_id != FieldType.ref:
        raise BadRequestError(f"column '{link}' of '{table.spec.slug}' is not a link (a ref column)")
    return column, RefConfig.model_validate(column.spec.config).table


def links_in(where: Where | None) -> Iterator[str]:
    match where:
        case Linked(link=name):
            yield name
        case And(all_of=nodes) | Or(any_of=nodes):
            for node in nodes:
                yield from links_in(node)
        case Not(negated=node):
            yield from links_in(node)
        case _:
            return


def uses_fuzzy(where: Where | None) -> bool:
    match where:
        case Comparison(op=op):
            return op is FilterOp.fuzzy
        case And(all_of=nodes) | Or(any_of=nodes):
            return any(uses_fuzzy(node) for node in nodes)
        case Not(negated=node) | Linked(where=node):
            return uses_fuzzy(node)
        case _:
            return False


def _joined(column: ResolvedColumn, rows: RowsEntity, target: RowsEntity) -> sa.ColumnElement[bool]:
    pointer = rows.payload[column.key]
    if column.spec.cardinality is Cardinality.many:
        return pointer.contains(sa.func.jsonb_build_array(sa.cast(target.id, sa.Text)))
    return target.id == sa.cast(pointer.astext, sa.UUID)


def compile_where(
    where: Where,
    table: ResolvedTable,
    targets: Mapping[str, ResolvedTable],
    rows: RowsEntity = GridRow,
    *,
    depth: int = 0,
) -> sa.ColumnElement[bool]:
    match where:
        case Comparison(column=column, op=op, value=value):
            if op in TEXT_PATTERN_OPS:
                if not isinstance(value, str):
                    raise BadRequestError(f"'{op.value}' on column '{column}' takes text")
                storable_text(value, f"the '{op.value}' pattern")
            node = SubstrateComparison(column=column, op=op, value=value)
            return compile_comparison(node, FieldSource(table, rows))
        case And(all_of=nodes):
            return sa.and_(*(compile_where(n, table, targets, rows, depth=depth) for n in nodes))
        case Or(any_of=nodes):
            return sa.or_(*(compile_where(n, table, targets, rows, depth=depth) for n in nodes))
        case Not(negated=node):
            return sa.not_(compile_where(node, table, targets, rows, depth=depth))
        case Linked(link=name, match=match, where=inner):
            if depth >= MAX_LINK_DEPTH:
                raise BadRequestError(f"a filter follows links at most {MAX_LINK_DEPTH} level deep ('{name}')")
            column, _ = ref_target(table, name)
            target = targets[name]
            linked = aliased(GridRow)
            condition = compile_where(inner, target, {}, linked, depth=depth + 1)
            candidates = sa.select(sa.literal(1)).where(
                linked.table_id == target.id, linked.archived_at.is_(None), _joined(column, rows, linked)
            )
            match match:
                case LinkMatch.some:
                    return sa.exists(candidates.where(condition))
                case LinkMatch.none:
                    return ~sa.exists(candidates.where(condition))
                case LinkMatch.every:
                    return ~sa.exists(candidates.where(~condition))
    raise BadRequestError(f"unsupported filter node: {where!r}")


def sort_order(table: ResolvedTable, sort: Sequence[SortKey]) -> SortOrder:
    source = FieldSource(table)
    out: SortOrder = []
    for key in sort:
        field = source.field(key.column)
        if not field.capabilities.sortable:
            raise BadRequestError(f"column '{key.column}' ({field.handler.type_id}) cannot be sorted")
        out.append((field, key.direction is SortDirection.desc))
    return out


def order_by(stmt: sa.Select[GridRow], order: SortOrder) -> sa.Select[GridRow]:
    by = [field.compared().desc() if desc else field.compared() for field, desc in order]
    ties_descending = bool(order) and order[-1][1]
    by.append(GridRow.id.desc() if ties_descending else GridRow.id.asc())
    return stmt.order_by(*by)


def after_cursor(order: SortOrder, boundary: list[JsonValue]) -> sa.ColumnElement[bool]:
    ascending = not any(desc for _, desc in order)
    try:
        levels: list[tuple[sa.SQLColumnExpression[Any], Any]] = [
            (field.compared(), None if value is None else field.literal(value))
            for (field, _), value in zip(order, boundary[:-1], strict=True)
        ]
        levels.append((GridRow.id, uuid.UUID(str(boundary[-1]))))
    except ValueError:
        raise BadRequestError("invalid cursor") from None
    return keyset_predicate(levels, ascending=ascending)


def leading_bound(order: SortOrder, boundary: list[JsonValue]) -> sa.ColumnElement[bool] | None:
    if not order or boundary[0] is None:
        return None
    field, descending = order[0]
    try:
        value = field.literal(boundary[0])
    except ValueError:
        raise BadRequestError("invalid cursor") from None
    return field.compared() <= value if descending else field.compared() >= value


def null_tail(order: SortOrder, boundary: list[JsonValue]) -> sa.ColumnElement[bool] | None:
    if not order or boundary[0] is None:
        return None
    field, descending = order[0]
    return None if descending or not field.nullable else field.compared().is_(None)


def cursor_of(last: GridRow, order: SortOrder) -> str:
    return encode_cursor([field.value_of(last) for field, _ in order] + [str(last.id)])


__all__ = [
    "MAX_LINK_DEPTH",
    "SortOrder",
    "after_cursor",
    "compile_where",
    "cursor_of",
    "leading_bound",
    "links_in",
    "null_tail",
    "order_by",
    "ref_target",
    "sort_order",
    "uses_fuzzy",
]
