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

from sqlalchemy.ext.asyncio import AsyncSession

from forktex_grid import Namespace
from forktex_grid.agent import TOOLS, run, tool_schemas


async def test_an_agent_designs_fills_and_questions_its_own_tables(session: AsyncSession) -> None:
    memory = Namespace(session, "agent-7f3a")
    assert {"apply", "batch", "query", "count", "field_types"} <= set(tool_schemas())

    types = (await run(memory, "field_types", {}))["result"]
    assert "gte" in types["decimal"]["capabilities"]["filterOps"]

    await run(
        memory,
        "batch",
        {
            "batch": {
                "desired": {
                    "tables": [
                        {"slug": "source", "columns": [{"key": "url", "typeId": "text", "isUnique": True}]},
                        {
                            "slug": "finding",
                            "columns": [
                                {"key": "claim", "typeId": "text", "isRequired": True},
                                {"key": "confidence", "typeId": "decimal"},
                                {"key": "source", "typeId": "ref", "config": {"table": "source"}},
                            ],
                        },
                    ]
                },
                "rows": [
                    {"op": "upsert", "table": "source", "externalRef": "s1", "values": {"url": "https://a.example"}}
                ],
            }
        },
    )
    source = (await run(memory, "get_by_external_ref", {"table": "source", "externalRef": "s1"}))["result"]
    await run(
        memory,
        "create_many",
        {
            "table": "finding",
            "values": [
                {"claim": "grid stores rows as JSONB", "confidence": 0.9, "source": source["id"]},
                {"claim": "grid needs a migration per tenant", "confidence": 0.1},
            ],
        },
    )

    answer = await run(
        memory,
        "query",
        {
            "table": "finding",
            "query": {
                "where": {"link": "source", "where": {"column": "url", "op": "startswith", "value": "https://"}},
                "select": ["claim", {"link": "source", "select": ["url"]}],
                "count": True,
            },
        },
    )
    (finding,) = answer["result"]["items"]
    assert finding["values"] == {"claim": "grid stores rows as JSONB"}
    assert finding["links"]["source"]["values"] == {"url": "https://a.example"}
    assert answer["result"]["total"] == 1
    assert TOOLS["query"].model_json_schema()["description"].startswith("Read a page of rows")
