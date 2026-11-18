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

from forktex.types import BaseWireValueObject
from pydantic import computed_field

from forktex_grid.domain.schema import ColumnSpec, IndexSpec, Schema, TableSpec


class ChangeOp(enum.StrEnum):
    create_table = "create_table"
    alter_table = "alter_table"
    add_column = "add_column"
    alter_column = "alter_column"
    create_index = "create_index"
    drop_index = "drop_index"
    drop_column = "drop_column"
    drop_table = "drop_table"


_ORDER = list(ChangeOp)


class Change(BaseWireValueObject):
    op: ChangeOp
    table: str
    key: str | None = None
    desired: TableSpec | ColumnSpec | IndexSpec | None = None
    actual: TableSpec | ColumnSpec | IndexSpec | None = None
    destructive: bool = False

    @computed_field
    @property
    def target(self) -> str:
        kind = self.op.value.split("_", 1)[1]
        return f"{kind}:{self.table}" if self.key is None else f"{kind}:{self.table}.{self.key}"


class ChangeSet(BaseWireValueObject):
    changes: tuple[Change, ...] = ()

    @property
    def destructive(self) -> tuple[Change, ...]:
        return tuple(c for c in self.changes if c.destructive)


def _tightens(desired: ColumnSpec, actual: ColumnSpec) -> bool:
    return (
        desired.type_id != actual.type_id
        or desired.cardinality != actual.cardinality
        or desired.config != actual.config
        or (desired.is_required and not actual.is_required)
        or (desired.is_unique and not actual.is_unique)
    )


def diff(actual: Schema, desired: Schema) -> ChangeSet:
    changes: list[Change] = []
    have = actual.by_slug
    for slug, want in desired.by_slug.items():
        cur = have.get(slug)
        if cur is None:
            changes.append(Change(op=ChangeOp.create_table, table=slug, desired=want))
            changes.extend(
                Change(op=ChangeOp.create_index, table=slug, key=i.signature, desired=i) for i in want.indexes
            )
            continue
        if (want.label, want.extends) != (cur.label, cur.extends):
            changes.append(
                Change(
                    op=ChangeOp.alter_table,
                    table=slug,
                    desired=want,
                    actual=cur,
                    destructive=want.extends != cur.extends,
                )
            )
        changes.extend(_columns(slug, want, cur))
        changes.extend(_indexes(slug, want, cur))
    changes.extend(
        Change(op=ChangeOp.drop_table, table=slug, actual=cur, destructive=True)
        for slug, cur in have.items()
        if slug not in desired.by_slug
    )
    changes.sort(key=lambda c: (_ORDER.index(c.op), c.table, c.key or ""))
    return ChangeSet(changes=tuple(changes))


def _columns(slug: str, want: TableSpec, cur: TableSpec) -> list[Change]:
    out: list[Change] = []
    have = {c.key: c for c in cur.columns}
    for col in want.columns:
        old = have.get(col.key)
        if old is None:
            fails_existing_rows = col.is_required and col.default_value is None
            out.append(
                Change(op=ChangeOp.add_column, table=slug, key=col.key, desired=col, destructive=fails_existing_rows)
            )
        elif old != col:
            out.append(
                Change(
                    op=ChangeOp.alter_column,
                    table=slug,
                    key=col.key,
                    desired=col,
                    actual=old,
                    destructive=_tightens(col, old),
                )
            )
    wanted = {c.key for c in want.columns}
    out.extend(
        Change(op=ChangeOp.drop_column, table=slug, key=key, actual=old, destructive=True)
        for key, old in have.items()
        if key not in wanted
    )
    return out


def _indexes(slug: str, want: TableSpec, cur: TableSpec) -> list[Change]:
    wanted = {i.signature: i for i in want.indexes}
    have = {i.signature: i for i in cur.indexes}
    return [
        Change(op=ChangeOp.create_index, table=slug, key=sig, desired=i) for sig, i in wanted.items() if sig not in have
    ] + [Change(op=ChangeOp.drop_index, table=slug, key=sig, actual=i) for sig, i in have.items() if sig not in wanted]


__all__ = ["Change", "ChangeOp", "ChangeSet", "diff"]
