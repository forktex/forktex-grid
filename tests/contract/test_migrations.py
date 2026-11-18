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

import asyncio
import shutil
from pathlib import Path

import pytest
import sqlalchemy as sa
from forktex.database.migrate import SchemaMigrationRunner
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from forktex_grid import ColumnSpec, Namespace, TableSpec, apply_migrations
from forktex_grid.persist.migrations import _migrations_dir
from tests.conftest import declare
from forktex_grid.persist.models import GridColumn, GridRow, GridTable

MODELS = (GridTable, GridColumn, GridRow)


async def _columns(engine: AsyncEngine, schema: str) -> dict[str, set[str]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            sa.text("select table_name, column_name from information_schema.columns where table_schema = :s"),
            {"s": schema},
        )
    out: dict[str, set[str]] = {}
    for table, column in rows.tuples():
        out.setdefault(table, set()).add(column)
    return out


def test_the_models_map_the_four_tables() -> None:
    assert [m.__table__.name for m in MODELS] == ["grid_table", "grid_column", "grid_row"]
    assert {m.__table__.schema for m in MODELS} == {"forktex_grid"}


async def test_the_migration_creates_exactly_what_the_orm_maps(grid_engine: AsyncEngine, grid_schema: str) -> None:
    found = await _columns(grid_engine, grid_schema)
    found.pop("schema_version", None)
    assert found == {m.__table__.name: {c.name for c in m.__table__.columns} for m in MODELS}


async def test_applying_the_migrations_again_changes_nothing(grid_engine: AsyncEngine, grid_schema: str) -> None:
    before = await _columns(grid_engine, grid_schema)
    await apply_migrations(grid_engine, schema=grid_schema)
    assert await _columns(grid_engine, grid_schema) == before
    async with grid_engine.connect() as conn:
        versions = (await conn.execute(sa.text(f'select version from "{grid_schema}".schema_version'))).scalars()
        assert list(versions) == [1]


async def test_an_upgrade_with_a_new_migration_keeps_every_row_and_runs_once_under_load(
    grid_engine: AsyncEngine, grid_schema: str, tmp_path: Path
) -> None:
    maker = async_sessionmaker(bind=grid_engine, expire_on_commit=False)
    spec = TableSpec(slug="item", columns=(ColumnSpec(key="code", type_id="text", is_unique=True),))
    async with maker() as s:
        await declare(Namespace(s, "upgrade"), spec)
        await (await Namespace(s, "upgrade").table("item")).create_many([{"code": f"c{i}"} for i in range(500)])
        await s.commit()

    for shipped in _migrations_dir().glob("v*.sql"):
        shutil.copy(shipped, tmp_path / shipped.name)
    (tmp_path / "v0002__next.sql").write_text(
        'CREATE INDEX ix_grid_row_created ON "{schema}".grid_row (created_at) WHERE archived_at IS NULL;\n'
    )

    async def boot() -> None:
        replica = create_async_engine(
            grid_engine.url, execution_options=grid_engine.get_execution_options(), pool_size=2, max_overflow=0
        )
        try:
            await SchemaMigrationRunner(engine=replica, schema=grid_schema, migrations_dir=tmp_path).apply()
        finally:
            await replica.dispose()

    async def write() -> int:
        written = 0
        for i in range(50):
            async with maker() as s:
                await (await Namespace(s, "upgrade").table("item")).create({"code": f"w{i}"})
                await s.commit()
                written += 1
        return written

    *_, written = await asyncio.gather(*(boot() for _ in range(3)), write())
    async with maker() as s:
        assert await (await Namespace(s, "upgrade").table("item")).count() == 500 + written
        versions = (await s.execute(sa.text(f'select version from "{grid_schema}".schema_version'))).scalars()
        assert list(versions) == [1, 2]
        index = await s.scalar(sa.text("select count(*) from pg_indexes where indexname = 'ix_grid_row_created'"))
        assert index == 1
    await boot()

    (tmp_path / "v0002__next.sql").write_text("SELECT 1;\n")
    with pytest.raises(Exception, match="(?i)checksum|changed|modified"):
        await boot()
