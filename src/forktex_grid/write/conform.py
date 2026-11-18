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
from collections.abc import AsyncIterator

import sqlalchemy as sa
from forktex.types import JsonValue
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_grid.domain.resolved import ResolvedColumn
from forktex_grid.domain.schema import ColumnSpec
from forktex_grid.domain.values import stored_value
from forktex_grid.errors import BadRequestError
from forktex_grid.persist.models import GridRow

SCAN_BATCH = 1000
REPORTED_ROWS = 10


async def backfill_default(session: AsyncSession, table_id: uuid.UUID, spec: ColumnSpec) -> None:
    if spec.default_value is None:
        return
    stored = stored_value(ResolvedColumn(spec), spec.default_value)
    await session.execute(
        sa.update(GridRow)
        .where(GridRow.table_id == table_id, GridRow.archived_at.is_(None), ~GridRow.payload.has_key(spec.key))
        .values(payload=GridRow.payload.op("||")(sa.bindparam("filled", {spec.key: stored}, type_=JSONB)))
    )


async def _live_values(
    session: AsyncSession, table_id: uuid.UUID, key: str
) -> AsyncIterator[tuple[uuid.UUID, JsonValue]]:
    after: uuid.UUID | None = None
    while True:
        stmt = sa.select(GridRow.id, GridRow.payload[key]).where(
            GridRow.table_id == table_id, GridRow.archived_at.is_(None)
        )
        if after is not None:
            stmt = stmt.where(GridRow.id > after)
        batch = (await session.execute(stmt.order_by(GridRow.id).limit(SCAN_BATCH))).all()
        for row_id, value in batch:
            yield row_id, value
        if len(batch) < SCAN_BATCH:
            return
        after = batch[-1][0]


async def ensure_rows_fit(session: AsyncSession, table_id: uuid.UUID, spec: ColumnSpec) -> None:
    column = ResolvedColumn(spec)
    misfits: list[str] = []
    reason = ""
    async for row_id, value in _live_values(session, table_id, spec.key):
        try:
            if value is None:
                if spec.is_required:
                    raise BadRequestError("it is required and the row has no value")
                continue
            stored_value(column, value)
        except BadRequestError as exc:
            misfits.append(str(row_id))
            reason = reason or str(exc)
    if misfits:
        raise BadRequestError(
            f"column '{spec.key}': {len(misfits)} existing row(s) do not fit the change ({reason}); "
            "fix or archive them first",
            details={"column": spec.key, "rows": misfits[:REPORTED_ROWS], "count": len(misfits)},
        )


__all__ = ["backfill_default", "ensure_rows_fit"]
