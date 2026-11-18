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
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_grid import AlreadyExistsError, BadRequestError, ColumnSpec, Namespace, NotFoundError, TableSpec
from tests.conftest import declare

NAME = ColumnSpec(key="name", type_id="text")


def _ref(key: str, table: str, on_delete: str = "restrict", **kw: Any) -> ColumnSpec:
    return ColumnSpec(key=key, type_id="ref", config={"table": table, "on_delete": on_delete}, **kw)


def _table(slug: str, *columns: ColumnSpec) -> TableSpec:
    return TableSpec(slug=slug, columns=(NAME, *columns))


async def test_a_ref_must_be_a_live_row_of_its_target_table(session: AsyncSession) -> None:
    clients, invoices, others = await declare(
        Namespace(session, "acme"), _table("client"), _table("invoice", _ref("client", "client")), _table("other")
    )
    client = await clients.create({"name": "ACME"})
    assert (await invoices.create({"client": client.id}))["client"] == str(client.id)

    stranger = await others.create({"name": "x"})
    for bad in (uuid.uuid7(), stranger.id):
        with pytest.raises(BadRequestError, match="'client'"):
            await invoices.create({"client": bad})

    gone = await clients.create({"name": "Gone"})
    await clients.archive(gone.id)
    with pytest.raises(BadRequestError, match="no live row"):
        await invoices.create({"client": gone.id})


async def test_a_ref_into_another_namespace_is_refused(session: AsyncSession) -> None:
    (theirs,) = await declare(Namespace(session, "b"), _table("client"))
    _, invoices = await declare(Namespace(session, "a"), _table("client"), _table("invoice", _ref("client", "client")))
    foreign = await theirs.create({"name": "B's"})
    with pytest.raises(BadRequestError):
        await invoices.create({"client": foreign.id})


async def test_a_many_ref_holds_a_list_of_live_rows(session: AsyncSession) -> None:
    tags, posts = await declare(
        Namespace(session, "acme"), _table("tag"), _table("post", _ref("tags", "tag", cardinality="many"))
    )
    a, b = await tags.create({"name": "a"}), await tags.create({"name": "b"})
    post = await posts.create({"tags": [a.id, b.id]})
    assert post["tags"] == [str(a.id), str(b.id)]
    with pytest.raises(BadRequestError):
        await posts.create({"tags": [a.id, uuid.uuid7()]})


async def test_restrict_refuses_archiving_a_referred_row_until_the_referrer_goes(session: AsyncSession) -> None:
    clients, invoices = await declare(
        Namespace(session, "acme"), _table("client"), _table("invoice", _ref("client", "client", "restrict"))
    )
    client = await clients.create({"name": "ACME"})
    invoice = await invoices.create({"client": client.id})
    with pytest.raises(BadRequestError, match="invoice.client"):
        await clients.archive(client.id)
    assert (await clients.get(client.id)).id == client.id
    await invoices.archive(invoice.id)
    await clients.archive(client.id)
    with pytest.raises(NotFoundError):
        await clients.get(client.id)


async def test_cascade_archives_referrers_transitively(session: AsyncSession) -> None:
    clients, invoices, lines = await declare(
        Namespace(session, "acme"),
        _table("client"),
        _table("invoice", _ref("client", "client", "cascade")),
        _table("line", _ref("invoice", "invoice", "cascade")),
    )
    client = await clients.create({"name": "ACME"})
    invoice = await invoices.create({"client": client.id})
    await lines.create({"invoice": invoice.id})
    await lines.create({"invoice": invoice.id})
    await clients.archive(client.id)
    assert (await invoices.query()).items == []
    assert (await lines.query()).items == []


async def test_cascade_stops_at_a_cycle(session: AsyncSession) -> None:
    (nodes,) = await declare(Namespace(session, "acme"), _table("node", _ref("next", "node", "cascade")))
    first = await nodes.create({"name": "1"})
    second = await nodes.create({"name": "2", "next": first.id})
    await nodes.patch(first.id, values={"next": second.id})
    await nodes.archive(first.id)
    assert (await nodes.query()).items == []


async def test_set_null_clears_a_one_ref(session: AsyncSession) -> None:
    clients, invoices = await declare(
        Namespace(session, "acme"), _table("client"), _table("invoice", _ref("client", "client", "set_null"))
    )
    client = await clients.create({"name": "ACME"})
    invoice = await invoices.create({"name": "INV", "client": client.id})
    await clients.archive(client.id)
    assert (await invoices.get(invoice.id))["client"] is None


async def test_set_null_removes_the_id_from_a_many_ref(session: AsyncSession) -> None:
    tags, posts = await declare(
        Namespace(session, "acme"),
        _table("tag"),
        _table("post", _ref("tags", "tag", "set_null", cardinality="many")),
    )
    a, b = await tags.create({"name": "a"}), await tags.create({"name": "b"})
    post = await posts.create({"tags": [a.id, b.id]})
    await tags.archive(a.id)
    assert (await posts.get(post.id))["tags"] == [str(b.id)]


async def test_set_null_is_refused_for_a_required_ref(session: AsyncSession) -> None:
    clients, invoices = await declare(
        Namespace(session, "acme"),
        _table("client"),
        _table("invoice", _ref("client", "client", "set_null", is_required=True)),
    )
    client = await clients.create({"name": "ACME"})
    await invoices.create({"client": client.id})
    with pytest.raises(BadRequestError, match="required"):
        await clients.archive(client.id)


async def test_a_unique_ref_is_one_to_one(session: AsyncSession) -> None:
    people, passports = await declare(
        Namespace(session, "acme"), _table("person"), _table("passport", _ref("holder", "person", is_unique=True))
    )
    ada = await people.create({"name": "Ada"})
    await passports.create({"holder": ada.id})
    with pytest.raises(AlreadyExistsError):
        await passports.create({"holder": ada.id})
