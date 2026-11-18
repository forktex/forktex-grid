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

from forktex_grid import (
    ApplyOptions,
    BadRequestError,
    IndexBuilds,
    Namespace,
    Query,
    Schema,
    col,
    link,
)

CRM = Schema.model_validate(
    {
        "tables": [
            {"slug": "company", "columns": [{"key": "name", "typeId": "text"}, {"key": "country", "typeId": "text"}]},
            {
                "slug": "deal",
                "columns": [
                    {"key": "title", "typeId": "text"},
                    {"key": "value", "typeId": "decimal"},
                    {"key": "company", "typeId": "ref", "config": {"table": "company", "onDelete": "cascade"}},
                ],
            },
        ]
    }
)


async def test_rows_link_filter_through_the_link_and_embed_what_they_point_at(session: AsyncSession) -> None:
    crm = Namespace(session, "crm")
    await crm.apply(CRM)
    companies, deals = await crm.table("company"), await crm.table("deal")
    acme = await companies.create({"name": "Acme", "country": "RO"})
    globex = await companies.create({"name": "Globex", "country": "DE"})
    await deals.create({"title": "Renewal", "value": "5000", "company": acme.id})
    await deals.create({"title": "Pilot", "value": "800", "company": globex.id})

    romanian = await deals.query(
        Query(where=link("company").where(col("country") == "RO"), select=("title", link("company").select("name")))
    )
    (deal,) = romanian.items
    assert deal.values == {"title": "Renewal"} and deal.links["company"].values == {"name": "Acme"}

    await companies.archive(globex.id)
    assert [d["title"] for d in (await deals.query()).items] == ["Renewal"]

    with pytest.raises(BadRequestError, match="refers to no live row"):
        await deals.create({"title": "Ghost", "company": globex.id})


async def test_a_schema_change_is_planned_before_it_is_made(session: AsyncSession) -> None:
    crm = Namespace(session, "crm")
    await crm.apply(CRM)
    smaller = Schema.model_validate({"tables": [CRM.tables[0].model_dump(mode="json")]})

    plan = await crm.apply(smaller, ApplyOptions(prune=True, dry_run=True))
    assert [c.target for c in plan.plan.changes] == ["table:deal"] and plan.plan.destructive

    with pytest.raises(BadRequestError, match="destructive"):
        await crm.apply(smaller, ApplyOptions(prune=True))
    await crm.apply(smaller, ApplyOptions(prune=True, allow_destructive=True, index_builds=IndexBuilds.inline))
    assert [t.slug for t in (await crm.describe()).tables] == ["company"]
