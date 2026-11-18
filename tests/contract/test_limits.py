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

import os

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_grid import BadRequestError, ColumnSpec, Namespace, Query, TableSpec, col
from tests.conftest import declare


@pytest.mark.parametrize(
    "spec",
    [
        lambda: TableSpec(slug="t" * 64),
        lambda: TableSpec(slug="t", label="l" * 256),
        lambda: TableSpec(slug="t", label="bad\x00label"),
        lambda: ColumnSpec(key="k" * 64, type_id="text"),
        lambda: ColumnSpec(key="k", label="l" * 256, type_id="text"),
        lambda: ColumnSpec(key="k", type_id="enum", config={"options": ["a\x00"]}),
        lambda: ColumnSpec(key="k", type_id="text", config={"subtype": "x\x00"}),
    ],
)
def test_a_spec_postgres_could_not_store_is_refused_before_it_reaches_it(spec: object) -> None:
    with pytest.raises(BadRequestError):
        spec()


@pytest.mark.parametrize("name", ["", "n" * 256, "tenant\x00"])
def test_a_namespace_postgres_could_not_store_is_refused(session: AsyncSession, name: str) -> None:
    with pytest.raises(BadRequestError):
        Namespace(session, name)


VALUES = TableSpec(
    slug="value",
    columns=(
        ColumnSpec(key="n", type_id="integer", is_unique=True),
        ColumnSpec(key="doc", type_id="json"),
        ColumnSpec(key="code", type_id="text", is_unique=True),
    ),
)


@pytest.mark.parametrize(
    "values",
    [
        {"n": 2**63},
        {"n": -(2**63) - 1},
        {"doc": {"a": "x\x00"}},
        {"doc": {"k\x00": 1}},
        {"code": os.urandom(8_000).hex()},
    ],
)
async def test_a_value_postgres_could_not_store_or_index_is_a_bad_request(session: AsyncSession, values: dict) -> None:
    (table,) = await declare(Namespace(session, "acme"), VALUES)
    with pytest.raises(BadRequestError):
        await table.create(values)
    assert await table.count() == 0


async def test_the_integer_range_is_the_full_64_bits(session: AsyncSession) -> None:
    (table,) = await declare(Namespace(session, "acme"), VALUES)
    await table.create({"n": 2**63 - 1})
    await table.create({"n": -(2**63)})
    assert await table.count(col("n") > 0) == 1


@pytest.mark.parametrize("external_ref", ["k" * 256, "key\x00"])
async def test_an_external_ref_postgres_could_not_store_is_refused(session: AsyncSession, external_ref: str) -> None:
    (table,) = await declare(Namespace(session, "acme"), VALUES)
    with pytest.raises(BadRequestError):
        await table.upsert_by_external_ref(external_ref, values={})
    with pytest.raises(BadRequestError):
        await table.get_by_external_ref(external_ref)


@pytest.mark.parametrize("pattern", [5, None, "a\x00"])
async def test_a_text_pattern_must_be_text(session: AsyncSession, pattern: object) -> None:
    (table,) = await declare(Namespace(session, "acme"), VALUES)
    with pytest.raises(BadRequestError):
        await table.query(Query(where=col("code").contains(pattern)))


@pytest.mark.parametrize("new_key", ["k" * 64, "id", "created_at", "bad key"])
async def test_a_rename_obeys_every_rule_a_column_key_does(session: AsyncSession, new_key: str) -> None:
    ns = Namespace(session, "acme")
    await declare(ns, VALUES)
    with pytest.raises(BadRequestError):
        await ns.rename_column("value", "code", new_key)
