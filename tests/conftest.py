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
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from sqlalchemy.engine import URL

from forktex_grid import ApplyOptions, IndexBuilds, Namespace, Schema, Table, TableSpec, apply_migrations
from tests._containers import start_postgres


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[URL]:
    container, url = start_postgres()
    yield url
    container.stop()


@pytest.fixture(scope="session")
def postgres_url_str(postgres_url: URL) -> str:
    return postgres_url.render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def grid_db_url(postgres_url_str: str) -> str:
    return postgres_url_str


@pytest_asyncio.fixture
async def fresh_schema(grid_db_url: str) -> AsyncIterator[str]:
    schema = "forktex_grid_test_" + uuid.uuid7().hex
    yield schema
    engine = create_async_engine(grid_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def grid_engine(grid_db_url: str, fresh_schema: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(
        grid_db_url,
        execution_options={"schema_translate_map": {"forktex_grid": fresh_schema}},
        pool_size=3,
        max_overflow=0,
    )
    await apply_migrations(engine, schema=fresh_schema)
    async with engine.begin() as conn:
        await conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    yield engine
    await engine.dispose()


@pytest.fixture
def grid_schema(fresh_schema: str) -> str:
    return fresh_schema


@pytest_asyncio.fixture
async def session(grid_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    maker = async_sessionmaker(bind=grid_engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as s:
        yield s


async def declare(ns: Namespace, *tables: TableSpec) -> list[Table]:
    await ns.apply(Schema(tables=tables), ApplyOptions(index_builds=IndexBuilds.inline))
    return [await ns.table(t.slug) for t in tables]
