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

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from forktex_grid import (
    AlreadyExistsError,
    ApplyOptions,
    BadRequestError,
    ColumnSpec,
    IndexBuilds,
    IndexSpec,
    Namespace,
    Schema,
    TableSpec,
)
from forktex_grid.persist.models import GridTable
from tests.conftest import declare

NS = "acme"
NAME = ColumnSpec(key="name", type_id="text")
CODE = ColumnSpec(key="code", type_id="text", is_unique=True)
TOTAL = ColumnSpec(key="total", type_id="decimal")


def table(
    slug: str, *columns: ColumnSpec, indexes: tuple[IndexSpec, ...] | list[IndexSpec] = (), extends: str | None = None
) -> TableSpec:
    return TableSpec(slug=slug, columns=columns, indexes=tuple(indexes), extends=extends)


async def _table_id(session: AsyncSession, ns: str, slug: str) -> uuid.UUID:
    return await session.scalar(sa.select(GridTable.id).where(GridTable.namespace == ns, GridTable.slug == slug))


async def _indexes(session: AsyncSession, schema: str, table_id: uuid.UUID) -> set[str]:
    rows = await session.execute(
        sa.text(
            "select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
            "where n.nspname = :s and c.relkind = 'i' and c.relname like :p"
        ),
        {"s": schema, "p": f"gx_{table_id.hex}_%"},
    )
    return set(rows.scalars())


async def test_apply_creates_tables_and_reapplying_describe_plans_nothing(session: AsyncSession) -> None:
    ns = Namespace(session, NS)
    report = await ns.apply(Schema(tables=[table("client", NAME, CODE)]), ApplyOptions(index_builds=IndexBuilds.inline))
    assert [c.target for c in report.plan.changes] == ["table:client"]
    described = await ns.describe()
    assert [t.slug for t in described.tables] == ["client"]
    again = await ns.apply(described, ApplyOptions(index_builds=IndexBuilds.inline))
    assert again.plan.changes == ()


async def test_a_patch_leaves_other_tables_and_prune_drops_them(session: AsyncSession) -> None:
    ns = Namespace(session, NS)
    await declare(ns, table("a", NAME), table("b", NAME))
    await ns.apply(Schema(tables=[table("a", NAME, TOTAL)]), ApplyOptions(index_builds=IndexBuilds.inline))
    assert sorted(t.slug for t in (await ns.describe()).tables) == ["a", "b"]
    await ns.apply(
        Schema(tables=[table("a", NAME, TOTAL)]),
        ApplyOptions(prune=True, allow_destructive=True, index_builds=IndexBuilds.inline),
    )
    assert [t.slug for t in (await ns.describe()).tables] == ["a"]


async def test_destructive_changes_need_allow_destructive_and_dry_run_applies_nothing(session: AsyncSession) -> None:
    ns = Namespace(session, NS)
    await declare(ns, table("a", NAME, TOTAL), table("b", NAME))
    target = Schema(tables=[table("a", NAME)])
    with pytest.raises(BadRequestError) as refused:
        await ns.apply(target, ApplyOptions(prune=True, index_builds=IndexBuilds.inline))
    assert "column:a.total" in str(refused.value) and "table:b" in str(refused.value)

    plan = await ns.apply(target, ApplyOptions(prune=True, dry_run=True))
    assert sorted(c.target for c in plan.plan.destructive) == ["column:a.total", "table:b"]
    assert sorted(t.slug for t in (await ns.describe()).tables) == ["a", "b"]
    assert [c.key for c in (await ns.describe()).table("a").columns] == ["name", "total"]


async def test_a_described_schema_applies_to_another_namespace_unchanged(session: AsyncSession) -> None:
    a, b = Namespace(session, "a"), Namespace(session, "b")
    await declare(
        a,
        table("client", NAME, CODE, indexes=[IndexSpec(column_keys=("name",))]),
        table("invoice", TOTAL, ColumnSpec(key="client", type_id="ref", config={"table": "client"})),
    )
    await b.apply(await a.describe(), ApplyOptions(index_builds=IndexBuilds.inline))
    assert await b.describe() == await a.describe()


@pytest.mark.parametrize(
    "changed",
    [
        ColumnSpec(key="total", type_id="integer"),
        ColumnSpec(key="total", type_id="decimal", cardinality="many"),
    ],
    ids=["type", "cardinality"],
)
async def test_a_retype_is_refused(session: AsyncSession, changed: ColumnSpec) -> None:
    ns = Namespace(session, NS)
    await declare(ns, table("a", TOTAL))
    with pytest.raises(BadRequestError, match="cannot change its type or cardinality"):
        await ns.apply(
            Schema(tables=[table("a", changed)]), ApplyOptions(allow_destructive=True, index_builds=IndexBuilds.inline)
        )


async def test_changing_what_a_table_extends_is_refused(session: AsyncSession) -> None:
    ns = Namespace(session, NS)
    await declare(ns, table("a", NAME))
    with pytest.raises(BadRequestError, match="cannot change what it extends"):
        await ns.apply(
            Schema(tables=[table("a", NAME, extends="public.client")]),
            ApplyOptions(allow_destructive=True, index_builds=IndexBuilds.inline),
        )


async def test_a_dropped_column_is_hidden_and_readding_it_starts_empty(session: AsyncSession) -> None:
    ns = Namespace(session, NS)
    (a,) = await declare(ns, table("a", NAME, TOTAL))
    row = await a.create({"name": "x", "total": "5"})
    await ns.apply(
        Schema(tables=[table("a", NAME)]),
        ApplyOptions(prune=True, allow_destructive=True, index_builds=IndexBuilds.inline),
    )
    a = await ns.table("a")
    assert (await a.get(row.id)).values == {"name": "x"}
    await ns.apply(Schema(tables=[table("a", NAME, TOTAL)]), ApplyOptions(index_builds=IndexBuilds.inline))
    a = await ns.table("a")
    assert (await a.get(row.id)).values == {"name": "x"}


async def test_rename_column_carries_values_and_indexes(session: AsyncSession, grid_schema: str) -> None:
    ns = Namespace(session, NS)
    (a,) = await declare(ns, table("a", NAME, CODE, indexes=[IndexSpec(column_keys=("code", "name"))]))
    row = await a.create({"name": "x", "code": "C1"})
    before = await _indexes(session, grid_schema, await _table_id(session, NS, "a"))

    await ns.rename_column("a", "code", "ref_code")
    a = await ns.table("a")
    assert (await a.get(row.id)).values == {"name": "x", "ref_code": "C1"}
    assert a.spec.indexes[0].column_keys == ("ref_code", "name")
    assert len(await _indexes(session, grid_schema, await _table_id(session, NS, "a"))) == len(before) == 2
    with pytest.raises(AlreadyExistsError):
        await a.create({"name": "y", "ref_code": "C1"})


async def test_indexes_exist_in_postgres_and_go_with_what_declared_them(
    session: AsyncSession, grid_schema: str
) -> None:
    ns = Namespace(session, NS)
    spec = table(
        "a",
        NAME,
        CODE,
        TOTAL,
        indexes=[IndexSpec(column_keys=("total",)), IndexSpec(column_keys=("name",), kind="trgm")],
    )
    await declare(ns, spec)
    tid = await _table_id(session, NS, "a")
    assert len(await _indexes(session, grid_schema, tid)) == 3

    await ns.apply(
        Schema(tables=[table("a", NAME, CODE, TOTAL)]),
        ApplyOptions(prune=True, allow_destructive=True, index_builds=IndexBuilds.inline),
    )
    assert len(await _indexes(session, grid_schema, tid)) == 1

    await ns.apply(
        Schema(tables=[table("a", NAME, TOTAL)]),
        ApplyOptions(prune=True, allow_destructive=True, index_builds=IndexBuilds.inline),
    )
    assert await _indexes(session, grid_schema, tid) == set()

    await ns.apply(
        Schema(tables=[table("a", NAME, CODE)]),
        ApplyOptions(prune=True, allow_destructive=True, index_builds=IndexBuilds.inline),
    )
    assert len(await _indexes(session, grid_schema, tid)) == 1
    await ns.apply(Schema(), ApplyOptions(prune=True, allow_destructive=True, index_builds=IndexBuilds.inline))
    assert await _indexes(session, grid_schema, tid) == set()


async def test_uniqueness_is_per_table_and_per_namespace(session: AsyncSession) -> None:
    a1, a2 = await declare(Namespace(session, "one"), table("a", CODE), table("b", CODE))
    (b1,) = await declare(Namespace(session, "two"), table("a", CODE))
    await a1.create({"code": "X"})
    await a2.create({"code": "X"})
    await b1.create({"code": "X"})
    with pytest.raises(AlreadyExistsError):
        await a1.create({"code": "X"})


async def test_a_concurrent_apply_commits_the_session(grid_engine: AsyncEngine) -> None:
    maker = async_sessionmaker(bind=grid_engine, expire_on_commit=False)
    async with maker() as s:
        report = await Namespace(s, NS).apply(Schema(tables=[table("a", CODE)]))
        assert report.options.index_builds is IndexBuilds.concurrent and report.complete
        assert [i.index for i in report.indexes] == ["unique btree(code)"]
    async with maker() as other:
        assert [t.slug for t in (await Namespace(other, NS).describe()).tables] == ["a"]


async def test_a_concurrent_apply_refuses_unflushed_changes(grid_engine: AsyncEngine) -> None:
    maker = async_sessionmaker(bind=grid_engine, expire_on_commit=False)
    async with maker() as s:
        s.add(GridTable(id=uuid.uuid7(), namespace="x", slug="pending", label="pending", indexes=[]))
        with pytest.raises(BadRequestError, match="unflushed"):
            await Namespace(s, NS).apply(Schema(tables=[table("a", NAME)]))


async def test_a_concurrent_apply_refuses_a_transaction_that_applied_inline(grid_engine: AsyncEngine) -> None:
    maker = async_sessionmaker(bind=grid_engine, expire_on_commit=False)
    async with maker() as s:
        ns = Namespace(s, NS)
        await ns.apply(Schema(tables=[table("a", NAME)]), ApplyOptions(index_builds=IndexBuilds.inline))
        with pytest.raises(BadRequestError, match="already holds this namespace.s schema lock"):
            await ns.apply(Schema(tables=[table("b", NAME)]))
        await s.rollback()


async def test_a_failed_concurrent_build_is_reported_and_rebuilt_once_the_data_is_fixed(
    grid_engine: AsyncEngine,
) -> None:
    maker = async_sessionmaker(bind=grid_engine, expire_on_commit=False)
    async with maker() as s:
        ns = Namespace(s, NS)
        (a,) = await declare(ns, table("a", NAME))
        first = await a.create({"name": "dup"})
        await a.create({"name": "dup"})
        await s.commit()

        unique = table("a", ColumnSpec(key="name", type_id="text", is_unique=True))
        report = await ns.apply(Schema(tables=[unique]), ApplyOptions(allow_destructive=True))
        assert not report.complete
        (failed,) = report.indexes
        assert (failed.ok, failed.failure, failed.columns) == (False, "duplicate_values", ("name",))
    async with maker() as s:
        ns = Namespace(s, NS)
        assert (await ns.describe()).table("a").column("name").is_unique
        await (await ns.table("a")).patch(first.id, values={"name": "unique now"})
        await s.commit()
        again = await ns.apply(Schema(tables=[unique]))
        assert again.complete and [i.ok for i in again.indexes] == [True]


async def test_an_inline_apply_over_duplicates_raises_and_applies_nothing(grid_engine: AsyncEngine) -> None:
    maker = async_sessionmaker(bind=grid_engine, expire_on_commit=False)
    async with maker() as s:
        ns = Namespace(s, NS)
        (a,) = await declare(ns, table("a", NAME))
        await a.create({"name": "dup"})
        await a.create({"name": "dup"})
        await s.commit()
        with pytest.raises(AlreadyExistsError):
            await ns.apply(
                Schema(tables=[table("a", ColumnSpec(key="name", type_id="text", is_unique=True))]),
                ApplyOptions(allow_destructive=True, index_builds=IndexBuilds.inline),
            )
        await s.rollback()
    async with maker() as s:
        assert not (await Namespace(s, NS).describe()).table("a").column("name").is_unique


async def test_a_link_never_changes_its_target_table_in_place(session: AsyncSession) -> None:
    ns = Namespace(session, "links")
    a = TableSpec(slug="a", columns=(ColumnSpec(key="n", type_id="text"),))
    b = TableSpec(slug="b", columns=(ColumnSpec(key="n", type_id="text"),))
    r = TableSpec(slug="r", columns=(ColumnSpec(key="to", type_id="ref", config={"table": "a"}),))
    _, _, refs = await declare(ns, a, b, r)
    retarget = r.model_copy(update={"columns": (ColumnSpec(key="to", type_id="ref", config={"table": "b"}),)})
    with pytest.raises(BadRequestError, match="cannot change which table it links to"):
        await ns.apply(
            Schema(tables=(retarget,)), ApplyOptions(allow_destructive=True, index_builds=IndexBuilds.inline)
        )
    keeps_target = r.model_copy(
        update={"columns": (ColumnSpec(key="to", type_id="ref", config={"table": "a", "onDelete": "cascade"}),)}
    )
    await ns.apply(
        Schema(tables=(keeps_target,)), ApplyOptions(allow_destructive=True, index_builds=IndexBuilds.inline)
    )


async def test_renaming_onto_a_dropped_columns_key_does_not_bring_its_values_back(session: AsyncSession) -> None:
    ns = Namespace(session, "rename-back")
    two = TableSpec(slug="t", columns=(ColumnSpec(key="a", type_id="text"), ColumnSpec(key="b", type_id="text")))
    (table,) = await declare(ns, two)
    row = await table.create({"a": "OLD-A"})
    only_b = TableSpec(slug="t", columns=(ColumnSpec(key="b", type_id="text"),))
    await ns.apply(Schema(tables=(only_b,)), ApplyOptions(allow_destructive=True, index_builds=IndexBuilds.inline))
    await ns.rename_column("t", "b", "a")
    assert (await (await ns.table("t")).get(row.id)).values == {}
