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
from datetime import date
from decimal import Decimal
from ipaddress import IPv4Address

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_grid import (
    BadRequestError,
    ColumnSpec,
    Namespace,
    Query,
    TableSpec,
    col,
    field_type,
    is_registered,
    register_field_type,
)
from tests.conftest import declare

INVOICE = TableSpec(
    slug="invoice",
    columns=(
        ColumnSpec(key="number", type_id="text", is_required=True, config={"maxLength": 12}),
        ColumnSpec(key="total", type_id="decimal", config={"precision": 10, "scale": 2}),
        ColumnSpec(key="issued", type_id="date"),
        ColumnSpec(key="status", type_id="enum", config={"options": ["draft", "sent"]}),
        ColumnSpec(key="lines", type_id="uuid", cardinality="many"),
        ColumnSpec(key="json", type_id="json"),
    ),
)


async def test_a_row_reads_back_as_python_values(session: AsyncSession) -> None:
    (invoices,) = await declare(Namespace(session, "acme"), INVOICE)
    line = uuid.uuid7()
    row = await invoices.create(
        {"number": "INV-1", "total": Decimal("250.5"), "issued": date(2026, 11, 1), "lines": [line], "json": {"a": 1}}
    )
    assert row.values["total"] == "250.5" and row.values["issued"] == "2026-11-01"
    typed = invoices.typed(row)
    assert typed.total == Decimal("250.5")
    assert typed.issued == date(2026, 11, 1)
    assert typed.lines == [line]
    assert typed.json_ == {"a": 1}
    assert typed.model_dump(mode="json", exclude_none=True) == row.values


async def test_the_table_model_validates_like_the_table(session: AsyncSession) -> None:
    (invoices,) = await declare(Namespace(session, "acme"), INVOICE)
    model = invoices.model()
    assert model is invoices.model()
    schema = model.model_json_schema()
    assert schema["required"] == ["number"]
    assert schema["properties"]["status"]["anyOf"][0]["enum"] == ["draft", "sent"]
    with pytest.raises(ValidationError):
        model.model_validate({"number": "far-too-long-a-number"})
    with pytest.raises(ValidationError):
        model.model_validate({"number": "INV-1", "status": "paid"})


async def test_a_pydantic_type_becomes_a_field_type(session: AsyncSession) -> None:
    if not is_registered("ipv4"):
        register_field_type(field_type("ipv4", IPv4Address))
    (hosts,) = await declare(
        Namespace(session, "acme"),
        TableSpec(slug="host", columns=(ColumnSpec(key="ip", type_id="ipv4", is_unique=True),)),
    )
    row = await hosts.create({"ip": IPv4Address("10.0.0.1")})
    assert row["ip"] == "10.0.0.1" and hosts.typed(row).ip == IPv4Address("10.0.0.1")
    with pytest.raises(BadRequestError, match="ip"):
        await hosts.create({"ip": "999.0.0.1"})
    assert [r["ip"] for r in (await hosts.query(Query(where=col("ip") == "10.0.0.1"))).items] == ["10.0.0.1"]
