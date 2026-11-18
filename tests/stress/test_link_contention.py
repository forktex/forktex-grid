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
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from forktex_grid import BadRequestError, ColumnSpec, ConflictError, Namespace, TableSpec, col, link
from tests.conftest import declare

TIMEOUT = 60
WRITERS = 8


def _ledger(on_delete: str) -> tuple[TableSpec, TableSpec]:
    clients = TableSpec(slug="client", columns=(ColumnSpec(key="name", type_id="text"),))
    invoices = TableSpec(
        slug="invoice",
        columns=(
            ColumnSpec(key="n", type_id="integer"),
            ColumnSpec(key="client", type_id="ref", config={"table": "client", "onDelete": on_delete}),
        ),
    )
    return clients, invoices


@pytest.mark.parametrize("on_delete", ["restrict", "cascade", "set_null"])
async def test_no_live_row_points_at_an_archived_row_however_writes_interleave(
    grid_engine: AsyncEngine, on_delete: str
) -> None:
    maker = async_sessionmaker(bind=grid_engine, expire_on_commit=False)
    ns = f"race-{uuid.uuid7().hex[:8]}"
    async with maker() as s:
        clients, _ = await declare(Namespace(s, ns), *_ledger(on_delete))
        target = await clients.create({"name": "Acme"})
        await s.commit()

    outcomes: list[str] = []

    async def write(n: int) -> None:
        async with maker() as s:
            try:
                await (await Namespace(s, ns).table("invoice")).create({"n": n, "client": target.id})
                await s.commit()
                outcomes.append("created")
            except BadRequestError, ConflictError:
                outcomes.append("refused")

    async def archive() -> None:
        await asyncio.sleep(0)
        async with maker() as s:
            try:
                await (await Namespace(s, ns).table("client")).archive(target.id)
                await s.commit()
                outcomes.append("archived")
            except BadRequestError, ConflictError:
                outcomes.append("kept")

    await asyncio.wait_for(asyncio.gather(*(write(n) for n in range(WRITERS)), archive()), TIMEOUT)

    async with maker() as s:
        namespace = Namespace(s, ns)
        live_clients = (await (await namespace.table("client")).query()).items
        invoices = await namespace.table("invoice")
        pointing = await invoices.count(link("client").where(col("id") == target.id))
        dangling = await invoices.count(col("client") == target.id) - pointing
    archived = not live_clients
    assert dangling == 0
    assert outcomes.count("created") + outcomes.count("refused") == WRITERS
    if archived:
        assert pointing == 0
        if on_delete == "restrict":
            assert outcomes.count("created") == 0
    else:
        assert pointing == outcomes.count("created")
