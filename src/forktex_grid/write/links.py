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
from collections.abc import Mapping, Sequence

import sqlalchemy as sa
from forktex.types import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_grid.domain.builtin_types import RefConfig
from forktex_grid.domain.enums import Cardinality, FieldType, OnDelete
from forktex_grid.domain.resolved import ResolvedTable
from forktex_grid.domain.values import linked_ids
from forktex_grid.errors import BadRequestError
from forktex_grid.persist import catalog
from forktex_grid.persist.models import GridColumn, GridRow, GridTable
from forktex_grid.read.rows import live, live_ids, locked


class Referrer:
    __slots__ = ("column", "table_id", "table_slug")

    def __init__(self, table_id: uuid.UUID, table_slug: str, column: GridColumn) -> None:
        self.table_id = table_id
        self.table_slug = table_slug
        self.column = column


async def referrers(session: AsyncSession, namespace: str, slug: str) -> list[Referrer]:
    rows = await session.execute(
        sa.select(GridTable.id, GridTable.slug, GridColumn)
        .join(GridColumn, GridColumn.table_id == GridTable.id)
        .where(
            GridTable.namespace == namespace,
            GridTable.archived_at.is_(None),
            GridColumn.archived_at.is_(None),
            GridColumn.type_id == FieldType.ref,
            GridColumn.config["table"].astext == slug,
        )
    )
    return [Referrer(table_id, table_slug, column) for table_id, table_slug, column in rows]


async def pointing_at(session: AsyncSession, referrer: Referrer, target: uuid.UUID) -> list[GridRow]:
    value = str(target) if referrer.column.cardinality == Cardinality.one else [str(target)]
    stmt = sa.select(GridRow).where(GridRow.payload.contains({referrer.column.key: value}), *live(referrer.table_id))
    return list(await session.scalars(locked(stmt, lock=True)))


async def ensure_targets_live(
    session: AsyncSession, table: ResolvedTable, payloads: Sequence[Mapping[str, JsonValue]]
) -> None:
    wanted: dict[str, tuple[str, set[uuid.UUID]]] = {}
    for key, col in table.columns.items():
        if col.spec.type_id != FieldType.ref:
            continue
        ids = {target_id for payload in payloads for target_id in linked_ids(payload.get(key))}
        if ids:
            wanted[key] = (RefConfig.model_validate(col.spec.config).table, ids)
    if not wanted:
        return
    targets = await catalog.table_ids(session, table.namespace)
    for key, (target, ids) in wanted.items():
        missing = ids - await live_ids(session, targets[target], sorted(ids)) if target in targets else ids
        if missing:
            raise BadRequestError(
                f"column '{key}' refers to no live row of '{target}'",
                details={"column": key, "rows": sorted(str(m) for m in missing)},
            )


class ArchivePlan:
    def __init__(self) -> None:
        self.archive: dict[uuid.UUID, tuple[str, GridRow]] = {}
        self.clear: list[tuple[GridRow, str, bool]] = []

    async def add(self, session: AsyncSession, namespace: str, slug: str, row: GridRow) -> None:
        if row.id in self.archive:
            return
        self.archive[row.id] = (slug, row)
        for referrer in await referrers(session, namespace, slug):
            column = referrer.column
            pointing = [p for p in await pointing_at(session, referrer, row.id) if p.id not in self.archive]
            if not pointing:
                continue
            match RefConfig.model_validate(column.config).on_delete:
                case OnDelete.restrict:
                    raise BadRequestError(
                        f"row is referred to by {referrer.table_slug}.{column.key} (on_delete restrict)",
                        details={
                            "table": referrer.table_slug,
                            "column": column.key,
                            "rows": [str(p.id) for p in pointing],
                        },
                    )
                case OnDelete.cascade:
                    for p in pointing:
                        await self.add(session, namespace, referrer.table_slug, p)
                case OnDelete.set_null:
                    many = column.cardinality == Cardinality.many
                    if column.is_required and not many:
                        raise BadRequestError(
                            f"row is referred to by required {referrer.table_slug}.{column.key} (on_delete set_null)"
                        )
                    self.clear.extend((p, column.key, many) for p in pointing)


__all__ = ["ArchivePlan", "Referrer", "ensure_targets_live", "pointing_at", "referrers"]
