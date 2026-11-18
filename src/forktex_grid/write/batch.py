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

from typing import Literal

from forktex.types import BaseWireValueObject
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_grid.domain.batch import ArchiveRow, Batch, CreateRow, PatchRow, RowOp, UpsertRow
from forktex_grid.domain.row import Row
from forktex_grid.persist import catalog
from forktex_grid.persist.tx import atomic, rehearsal
from forktex_grid.write import rows
from forktex_grid.write.apply import ApplyOptions, ApplyReport, apply_schema, in_one_transaction


class RowResult(BaseWireValueObject):
    op: Literal["create", "upsert", "patch", "archive"]
    table: str
    row: Row | None = None


class BatchReport(BaseWireValueObject):
    schema_report: ApplyReport | None = None
    results: tuple[RowResult, ...] = ()
    dry_run: bool = False


async def _run(session: AsyncSession, namespace: str, op: RowOp) -> RowResult:
    ref = await catalog.load_table(session, namespace, op.table)
    row: Row | None = None
    match op:
        case CreateRow():
            row = (await rows.create(session, ref, [op.values], [op.external_ref]))[0]
        case UpsertRow():
            row = await rows.upsert(session, ref, op.external_ref, op.values)
        case PatchRow():
            row = await rows.patch(session, ref, op.row_id, op.values)
        case ArchiveRow():
            await rows.archive(session, ref, op.row_id)
    return RowResult(op=op.op, table=op.table, row=row)


async def _execute(
    session: AsyncSession, namespace: str, batch: Batch, options: ApplyOptions
) -> tuple[ApplyReport | None, tuple[RowResult, ...]]:
    async with atomic(session):
        report = None if batch.desired is None else await apply_schema(session, namespace, batch.desired, options)
        results = tuple([await _run(session, namespace, op) for op in batch.rows])
    return report, results


async def apply_batch(session: AsyncSession, namespace: str, batch: Batch, options: ApplyOptions) -> BatchReport:
    options = in_one_transaction(options, "a batch")
    if not options.dry_run:
        report, results = await _execute(session, namespace, batch, options)
        return BatchReport(schema_report=report, results=results)
    async with rehearsal(session):
        report, results = await _execute(session, namespace, batch, options.model_copy(update={"dry_run": False}))
    if report is not None:
        report = report.model_copy(update={"options": options})
    return BatchReport(schema_report=report, results=results, dry_run=True)


__all__ = ["BatchReport", "RowResult", "apply_batch"]
