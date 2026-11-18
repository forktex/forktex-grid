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
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_grid import (
    ApplyOptions,
    ArchiveRow,
    BadRequestError,
    Batch,
    ColumnSpec,
    CreateRow,
    IndexBuilds,
    Namespace,
    NotFoundError,
    PatchRow,
    Query,
    Schema,
    TableSpec,
    UpsertRow,
)

STAGES = TableSpec(slug="stage", columns=(ColumnSpec(key="name", type_id="text", is_required=True),))


async def test_rows_seed_a_table_created_in_the_same_batch(session: AsyncSession) -> None:
    ns = Namespace(session, "acme")
    report = await ns.batch(
        Batch(
            desired=Schema(tables=(STAGES,)),
            rows=(CreateRow(table="stage", values={"name": "lead"}), CreateRow(table="stage", values={"name": "won"})),
        )
    )
    assert report.schema_report is not None
    assert [r.row["name"] for r in report.results if r.row] == ["lead", "won"]
    assert [r.op for r in report.results] == ["create", "create"]
    assert await (await ns.table("stage")).count() == 2


async def test_a_batch_document_arrives_as_json(session: AsyncSession) -> None:
    document = {
        "desired": {"tables": [{"slug": "stage", "columns": [{"key": "name", "typeId": "text"}]}]},
        "rows": [
            {"op": "create", "table": "stage", "values": {"name": "lead"}},
            {"op": "upsert", "table": "stage", "externalRef": "s-2", "values": {"name": "won"}},
        ],
    }
    batch = Batch.model_validate(document)
    assert Batch.model_validate(batch.model_dump(mode="json")) == batch
    report = await Namespace(session, "acme").batch(batch)
    assert [r.op for r in report.results] == ["create", "upsert"]


def test_a_batch_document_is_validated() -> None:
    with pytest.raises(ValidationError):
        Batch.model_validate({"rows": [{"op": "explode", "table": "stage"}]})
    with pytest.raises(ValidationError):
        Batch.model_validate({"rows": [{"op": "patch", "table": "stage", "values": {"name": "x"}}]})
    with pytest.raises(ValidationError):
        Batch.model_validate({"rows": [], "schema": {}})


async def test_a_failing_op_rolls_back_the_schema_and_the_earlier_ops(session: AsyncSession) -> None:
    ns = Namespace(session, "acme")
    with pytest.raises(BadRequestError):
        await ns.batch(
            Batch(
                desired=Schema(tables=(STAGES,)),
                rows=(CreateRow(table="stage", values={"name": "lead"}), CreateRow(table="stage", values={})),
            )
        )
    with pytest.raises(NotFoundError):
        await ns.table("stage")
    assert (await ns.describe()).tables == ()


async def test_a_dry_run_rehearses_schema_and_rows_and_keeps_nothing(session: AsyncSession) -> None:
    ns = Namespace(session, "acme")
    batch = Batch(desired=Schema(tables=(STAGES,)), rows=(CreateRow(table="stage", values={"name": "x"}),))
    report = await ns.batch(batch, ApplyOptions(dry_run=True))
    assert report.dry_run is True
    assert report.schema_report is not None and report.schema_report.options.dry_run
    assert [c.target for c in report.schema_report.plan.changes] == ["table:stage"]
    assert [r.row["name"] for r in report.results if r.row] == ["x"]
    assert (await ns.describe()).tables == ()
    real = await ns.batch(batch)
    assert real.dry_run is False and await (await ns.table("stage")).count() == 1


async def test_a_dry_run_raises_what_the_real_batch_would_and_keeps_the_rest(session: AsyncSession) -> None:
    ns = Namespace(session, "acme")
    await ns.batch(Batch(desired=Schema(tables=(STAGES,)), rows=(CreateRow(table="stage", values={"name": "a"}),)))
    stages = await ns.table("stage")
    (row,) = (await stages.query()).items
    rehearsal = Batch(
        rows=(PatchRow(table="stage", row_id=row.id, values={"name": "b"}), CreateRow(table="stage", values={}))
    )
    with pytest.raises(BadRequestError, match="'name' is required"):
        await ns.batch(rehearsal, ApplyOptions(dry_run=True))
    assert (await stages.get(row.id))["name"] == "a"
    archive_it = Batch(rows=(ArchiveRow(table="stage", row_id=row.id),))
    assert (await ns.batch(archive_it, ApplyOptions(dry_run=True))).results[0].op == "archive"
    assert await stages.count() == 1


async def test_a_batch_refuses_concurrent_index_builds(session: AsyncSession) -> None:
    ns = Namespace(session, "acme")
    with pytest.raises(BadRequestError, match="inline"):
        await ns.batch(Batch(desired=Schema(tables=(STAGES,))), ApplyOptions(index_builds=IndexBuilds.concurrent))


async def test_upsert_patch_and_archive_ops(session: AsyncSession) -> None:
    ns = Namespace(session, "acme")
    await ns.batch(Batch(desired=Schema(tables=(STAGES,))))
    created = await ns.batch(Batch(rows=(UpsertRow(table="stage", external_ref=7, values={"name": "lead"}),)))
    row = created.results[0].row
    assert row is not None and row.external_ref == "7"
    report = await ns.batch(
        Batch(
            rows=(
                UpsertRow(table="stage", external_ref="7", values={"name": "qualified"}),
                PatchRow(table="stage", row_id=row.id, values={"name": "won"}),
                ArchiveRow(table="stage", row_id=row.id),
            )
        )
    )
    assert [r.op for r in report.results] == ["upsert", "patch", "archive"]
    assert report.results[0].row is not None and report.results[0].row.id == row.id
    assert report.results[1].row is not None and report.results[1].row["name"] == "won"
    assert report.results[2].row is None
    assert (await (await ns.table("stage")).query(Query())).items == []
