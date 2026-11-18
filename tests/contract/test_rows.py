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

from forktex_grid import (
    AlreadyExistsError,
    BadRequestError,
    ColumnSpec,
    Namespace,
    NotFoundError,
    TableSpec,
)
from tests.conftest import declare

PEOPLE = TableSpec(
    slug="person",
    columns=(
        ColumnSpec(key="name", type_id="text", is_required=True),
        ColumnSpec(key="age", type_id="integer"),
        ColumnSpec(key="balance", type_id="decimal"),
        ColumnSpec(key="born", type_id="date"),
        ColumnSpec(key="active", type_id="boolean", default_value=True),
        ColumnSpec(key="email", type_id="text", is_unique=True),
        ColumnSpec(key="tags", type_id="enum", cardinality="many", config={"options": ["a", "b"]}),
    ),
)


async def _people(session: AsyncSession, namespace: str = "acme"):
    (people,) = await declare(Namespace(session, namespace), PEOPLE)
    return people


async def test_values_are_stored_in_their_canonical_form(session: AsyncSession) -> None:
    people = await _people(session)
    row = await people.create({"name": "Ada", "age": "42", "balance": 10.5, "born": "2001-02-03", "tags": ["a"]})
    assert row.values == {
        "name": "Ada",
        "age": 42,
        "balance": "10.5",
        "born": "2001-02-03",
        "active": True,
        "tags": ["a"],
    }
    assert isinstance(row.id, uuid.UUID)
    assert row.external_ref is None
    assert row.created_at is not None and row.updated_at is not None
    assert (await people.get(row.id)).values == row.values


async def test_an_unknown_column_is_refused_by_name(session: AsyncSession) -> None:
    people = await _people(session)
    with pytest.raises(BadRequestError, match="nickname"):
        await people.create({"name": "Ada", "nickname": "A"})


async def test_a_value_the_type_refuses_names_the_column(session: AsyncSession) -> None:
    people = await _people(session)
    with pytest.raises(BadRequestError, match="'age'"):
        await people.create({"name": "Ada", "age": "old"})


async def test_a_required_column_is_needed_on_create_and_cannot_be_cleared(session: AsyncSession) -> None:
    people = await _people(session)
    with pytest.raises(BadRequestError, match="'name' is required"):
        await people.create({"age": 1})
    row = await people.create({"name": "Ada"})
    with pytest.raises(BadRequestError, match="'name' is required"):
        await people.patch(row.id, values={"name": None})


async def test_a_default_is_filled_on_create_only(session: AsyncSession) -> None:
    people = await _people(session)
    row = await people.create({"name": "Ada", "active": False})
    assert row["active"] is False
    patched = await people.patch(row.id, values={"age": 3})
    assert patched["active"] is False
    assert (await people.create({"name": "Bob"}))["active"] is True


async def test_a_many_column_takes_a_list_and_refuses_a_scalar(session: AsyncSession) -> None:
    people = await _people(session)
    assert (await people.create({"name": "Ada", "tags": ["a", "b"]}))["tags"] == ["a", "b"]
    with pytest.raises(BadRequestError, match="'tags'"):
        await people.create({"name": "Bob", "tags": "a"})
    with pytest.raises(BadRequestError, match="'tags'"):
        await people.create({"name": "Bob", "tags": ["c"]})


async def test_create_many_writes_every_row(session: AsyncSession) -> None:
    people = await _people(session)
    created = await people.create_many([{"name": "Ada"}, {"name": "Bob"}])
    assert [r["name"] for r in created] == ["Ada", "Bob"]
    assert await people.count() == 2


async def test_patch_sets_only_the_given_columns(session: AsyncSession) -> None:
    people = await _people(session)
    row = await people.create({"name": "Ada", "age": 30})
    patched = await people.patch(row.id, values={"age": 31})
    assert patched["name"] == "Ada" and patched["age"] == 31
    assert patched.id == row.id


async def test_an_archived_row_is_gone_from_every_read(session: AsyncSession) -> None:
    people = await _people(session)
    row = await people.create({"name": "Ada"})
    await people.archive(row.id)
    with pytest.raises(NotFoundError):
        await people.get(row.id)
    with pytest.raises(NotFoundError):
        await people.patch(row.id, values={"age": 1})
    assert (await people.query()).items == []


async def test_a_row_of_another_table_is_not_found(session: AsyncSession) -> None:
    people, others = await declare(
        Namespace(session, "acme"), PEOPLE, TableSpec(slug="other", columns=(ColumnSpec(key="x", type_id="text"),))
    )
    row = await others.create({"x": "1"})
    with pytest.raises(NotFoundError):
        await people.get(row.id)
    with pytest.raises(NotFoundError):
        await people.get(uuid.uuid7())


async def test_a_duplicate_unique_value_is_refused_naming_the_table(session: AsyncSession) -> None:
    people = await _people(session)
    await people.create({"name": "Ada", "email": "a@x"})
    with pytest.raises(AlreadyExistsError, match="person"):
        await people.create({"name": "Bob", "email": "a@x"})
    other = await people.create({"name": "Cy", "email": "c@x"})
    with pytest.raises(AlreadyExistsError, match="person"):
        await people.patch(other.id, values={"email": "a@x"})


async def test_a_unique_value_is_unique_per_namespace(session: AsyncSession) -> None:
    a = await _people(session, "a")
    b = await _people(session, "b")
    await a.create({"name": "Ada", "email": "same@x"})
    await b.create({"name": "Ada", "email": "same@x"})
    with pytest.raises(AlreadyExistsError):
        await a.create({"name": "Eve", "email": "same@x"})


async def test_an_archived_row_frees_its_unique_value(session: AsyncSession) -> None:
    people = await _people(session)
    row = await people.create({"name": "Ada", "email": "a@x"})
    await people.archive(row.id)
    bob = await people.create({"name": "Bob", "email": "a@x"})
    assert (await people.get(bob.id))["email"] == "a@x"
