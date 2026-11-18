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

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from forktex_grid import (
    ApplyOptions,
    BadRequestError,
    ColumnSpec,
    IndexBuilds,
    IndexSpec,
    Namespace,
    Query,
    Schema,
    TableSpec,
    apply_migrations,
    col,
)

PEOPLE = TableSpec(slug="person", columns=(ColumnSpec(key="name", type_id="text"),))
SEARCHABLE = PEOPLE.model_copy(update={"indexes": (IndexSpec(column_keys=("name",), kind="trgm"),)})


@pytest_asyncio.fixture
async def without_trgm(postgres_url: URL) -> AsyncIterator[AsyncSession]:
    name = "no_trgm_" + uuid.uuid7().hex
    admin = create_async_engine(postgres_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    engine = create_async_engine(postgres_url.set(database=name))
    try:
        await apply_migrations(engine)
        async with async_sessionmaker(bind=engine, expire_on_commit=False)() as session:
            yield session
    finally:
        await engine.dispose()
        async with admin.connect() as conn:
            await conn.execute(sa.text(f'DROP DATABASE "{name}"'))
        await admin.dispose()


async def test_a_trgm_index_is_refused_before_anything_is_built(without_trgm: AsyncSession) -> None:
    ns = Namespace(without_trgm, "search")
    for options in (ApplyOptions(), ApplyOptions(index_builds=IndexBuilds.inline), ApplyOptions(dry_run=True)):
        with pytest.raises(BadRequestError, match="a trgm index needs the pg_trgm extension"):
            await ns.apply(Schema(tables=(SEARCHABLE,)), options)
    assert (await ns.describe()).tables == ()


async def test_the_fuzzy_filter_is_refused_with_the_same_reason(without_trgm: AsyncSession) -> None:
    ns = Namespace(without_trgm, "search")
    await ns.apply(Schema(tables=(PEOPLE,)), ApplyOptions(index_builds=IndexBuilds.inline))
    people = await ns.table("person")
    await people.create({"name": "Ionescu"})
    with pytest.raises(BadRequestError, match="the 'fuzzy' filter needs the pg_trgm extension"):
        await people.query(Query(where=col("name").fuzzy("Ionesku")))
    with pytest.raises(BadRequestError, match="pg_trgm"):
        await people.count(col("name").fuzzy("Ionesku") | (col("name") == "x"))
    assert await people.count(col("name") == "Ionescu") == 1


async def test_pg_trgm_off_the_search_path_is_refused_with_the_reason(without_trgm: AsyncSession) -> None:
    await without_trgm.execute(sa.text("CREATE SCHEMA hidden_ext"))
    await without_trgm.execute(sa.text("CREATE EXTENSION pg_trgm SCHEMA hidden_ext"))
    await without_trgm.commit()
    ns = Namespace(without_trgm, "search")
    with pytest.raises(BadRequestError, match="on the search path"):
        await ns.apply(Schema(tables=(SEARCHABLE,)), ApplyOptions(index_builds=IndexBuilds.inline))
    await ns.apply(Schema(tables=(PEOPLE,)), ApplyOptions(index_builds=IndexBuilds.inline))
    with pytest.raises(BadRequestError, match="on the search path"):
        await (await ns.table("person")).query(Query(where=col("name").fuzzy("x")))
    await without_trgm.rollback()
    await without_trgm.execute(sa.text("SET search_path TO public, hidden_ext"))
    await ns.apply(Schema(tables=(SEARCHABLE,)), ApplyOptions(index_builds=IndexBuilds.inline))
    await (await ns.table("person")).create({"name": "Ionescu"})
    assert len((await (await ns.table("person")).query(Query(where=col("name").fuzzy("Ionesku")))).items) == 1
