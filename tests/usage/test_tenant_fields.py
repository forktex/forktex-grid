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
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_grid import BadRequestError, ColumnSpec, Namespace, Query, Schema, TableSpec, col

CLIENT_FIELDS = TableSpec(
    slug="client_fields",
    label="Client",
    extends="public.client",
    columns=[
        ColumnSpec(key="bookkeeper", label="Bookkeeper", type_id="text", display_order=1),
        ColumnSpec(key="mandate", label="Power of attorney", type_id="enum", config={"options": ["yes", "no"]}),
        ColumnSpec(key="mandate_year", label="Mandate year", type_id="integer"),
    ],
)


async def test_each_tenant_adds_its_own_typed_fields_to_the_hosts_clients(session: AsyncSession) -> None:
    clients_of_acme = [uuid.uuid7() for _ in range(3)]
    acme = Namespace(session, "tenant-acme")
    await acme.apply(Schema(tables=[CLIENT_FIELDS]))
    fields = await acme.table("client_fields")

    await fields.upsert_by_external_ref(clients_of_acme[0], values={"bookkeeper": "IP", "mandate": "yes"})
    await fields.upsert_by_external_ref(clients_of_acme[0], values={"mandate_year": 2024})
    await fields.upsert_by_external_ref(clients_of_acme[1], values={"bookkeeper": "AM"})

    on_screen = await fields.get_many_by_external_ref(clients_of_acme)
    assert on_screen[str(clients_of_acme[0])].values == {"bookkeeper": "IP", "mandate": "yes", "mandate_year": 2024}
    assert str(clients_of_acme[2]) not in on_screen

    with_mandate = await fields.query(Query(where=col("mandate") == "yes", select=("bookkeeper",)))
    assert [r.external_ref for r in with_mandate.items] == [str(clients_of_acme[0])]

    with pytest.raises(BadRequestError, match="mandate"):
        await fields.upsert_by_external_ref(clients_of_acme[2], values={"mandate": "maybe"})


async def test_a_form_validates_with_the_tables_own_model_before_writing(session: AsyncSession) -> None:
    acme = Namespace(session, "tenant-acme")
    await acme.apply(Schema(tables=[CLIENT_FIELDS]))
    form = (await acme.table("client_fields")).model()

    assert form.model_json_schema()["properties"]["mandate"]["title"] == "Power of attorney"
    with pytest.raises(ValidationError):
        form.model_validate({"mandate_year": "next year"})


async def test_another_tenant_sees_nothing_of_it(session: AsyncSession) -> None:
    await Namespace(session, "tenant-acme").apply(Schema(tables=[CLIENT_FIELDS]))
    assert (await Namespace(session, "tenant-globex").describe()).tables == ()
