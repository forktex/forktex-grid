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
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_grid import BadRequestError, ColumnSpec, Namespace, NotFoundError, TableSpec
from tests.conftest import declare

CLIENT = TableSpec(slug="client", columns=[ColumnSpec(key="name", type_id="text")])
INVOICE = TableSpec(slug="invoice", columns=[ColumnSpec(key="client", type_id="ref", config={"table": "client"})])


async def test_the_same_slugs_coexist_in_two_namespaces(session: AsyncSession) -> None:
    (a,) = await declare(Namespace(session, "a"), CLIENT)
    (b,) = await declare(Namespace(session, "b"), CLIENT)
    await a.create({"name": "A"})
    assert [r["name"] for r in (await a.query()).items] == ["A"]
    assert (await b.query()).items == []


async def test_a_row_of_another_namespace_is_not_found(session: AsyncSession) -> None:
    (a,) = await declare(Namespace(session, "a"), CLIENT)
    (b,) = await declare(Namespace(session, "b"), CLIENT)
    row = await a.create({"name": "A"})
    with pytest.raises(NotFoundError):
        await b.get(row.id)
    with pytest.raises(NotFoundError):
        await b.patch(row.id, values={"name": "taken"})
    with pytest.raises(NotFoundError):
        await b.archive(row.id)
    assert (await a.get(row.id))["name"] == "A"


async def test_a_ref_cannot_point_into_another_namespace(session: AsyncSession) -> None:
    (a_client,) = await declare(Namespace(session, "a"), CLIENT)
    _, b_invoice = await declare(Namespace(session, "b"), CLIENT, INVOICE)
    theirs = await a_client.create({"name": "A"})
    with pytest.raises(BadRequestError, match="refers to no live row"):
        await b_invoice.create({"client": str(theirs.id)})


async def test_describe_shows_only_its_own_namespace(session: AsyncSession) -> None:
    await declare(Namespace(session, "a"), CLIENT, INVOICE)
    await declare(Namespace(session, "b"), TableSpec(slug="other"))
    assert [t.slug for t in (await Namespace(session, "b").describe()).tables] == ["other"]
    with pytest.raises(NotFoundError):
        await Namespace(session, "b").table("client")
