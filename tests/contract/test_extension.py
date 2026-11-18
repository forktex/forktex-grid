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
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_grid import AlreadyExistsError, BadRequestError, ColumnSpec, Namespace, TableSpec
from tests.conftest import declare

FIELDS = TableSpec(
    slug="client_fields",
    extends="public.host",
    columns=(ColumnSpec(key="tier", type_id="text"), ColumnSpec(key="manager", type_id="text")),
)


async def _fields(session: AsyncSession):
    (fields,) = await declare(Namespace(session, "acme"), FIELDS)
    return fields


async def test_a_row_of_an_extension_needs_its_host_key(session: AsyncSession) -> None:
    fields = await _fields(session)
    with pytest.raises(BadRequestError, match="external_ref"):
        await fields.create({"tier": "gold"})
    with pytest.raises(BadRequestError, match="external_ref"):
        await fields.create_many([{"tier": "gold"}, {"tier": "x"}], external_refs=["a", None])
    assert (await fields.create({"tier": "gold"}, external_ref="h1")).external_ref == "h1"


async def test_upsert_creates_then_patches_the_same_row(session: AsyncSession) -> None:
    fields = await _fields(session)
    host = uuid.uuid7()
    first = await fields.upsert_by_external_ref(host, values={"tier": "gold"})
    second = await fields.upsert_by_external_ref(host, values={"manager": "IP"})
    assert second.id == first.id
    assert second.values == {"tier": "gold", "manager": "IP"}
    assert second.external_ref == str(host)


async def test_host_keys_of_any_type_are_stored_as_text(session: AsyncSession) -> None:
    fields = await _fields(session)
    key = uuid.uuid7()
    for host in (42, key, "abc"):
        await fields.create({"tier": "x"}, external_ref=host)
    found = await fields.get_by_external_ref(42)
    assert found is not None and found.external_ref == "42"
    assert (await fields.get_by_external_ref("42")) is not None
    assert (await fields.get_by_external_ref(str(key))) is not None
    assert await fields.get_by_external_ref("unknown") is None


async def test_get_many_returns_the_linked_rows_by_text_key(session: AsyncSession) -> None:
    fields = await _fields(session)
    await fields.create({"tier": "gold"}, external_ref=1)
    await fields.create({"tier": "silver"}, external_ref=2)
    found = await fields.get_many_by_external_ref([1, 2, 3])
    assert sorted(found) == ["1", "2"]
    assert found["2"]["tier"] == "silver"
    assert await fields.get_many_by_external_ref([]) == {}


async def test_a_host_key_links_one_live_row_per_table(session: AsyncSession) -> None:
    fields = await _fields(session)
    row = await fields.create({"tier": "gold"}, external_ref="h")
    with pytest.raises(AlreadyExistsError):
        await fields.create({"tier": "silver"}, external_ref="h")
    await fields.archive(row.id)
    assert (await fields.create({"tier": "silver"}, external_ref="h"))["tier"] == "silver"


async def test_a_plain_table_may_carry_a_host_key_too(session: AsyncSession) -> None:
    (notes,) = await declare(
        Namespace(session, "acme"), TableSpec(slug="note", columns=(ColumnSpec(key="t", type_id="text"),))
    )
    assert (await notes.create({"t": "x"})).external_ref is None
    linked = await notes.create({"t": "y"}, external_ref="ticket-7")
    found = await notes.get_by_external_ref("ticket-7")
    assert found is not None and found.id == linked.id
