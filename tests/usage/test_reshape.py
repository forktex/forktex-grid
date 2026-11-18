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

from forktex_grid import (
    ApplyOptions,
    ColumnSpec,
    IndexBuilds,
    Migration,
    Namespace,
    Schema,
    TableSource,
    TableSpec,
    ValueCount,
)

OLD = Schema(
    tables=[
        TableSpec(
            slug="contact", columns=[ColumnSpec(key="full_name", type_id="text"), ColumnSpec(key="fax", type_id="text")]
        ),
        TableSpec(
            slug="appointment",
            columns=[
                ColumnSpec(key="title", type_id="text"),
                ColumnSpec(key="with", type_id="ref", config={"table": "contact"}),
            ],
        ),
        TableSpec(slug="task", columns=[ColumnSpec(key="title", type_id="text")]),
    ]
)
NEW = Schema(
    tables=[
        TableSpec(slug="person", columns=[ColumnSpec(key="name", type_id="text", is_required=True)]),
        TableSpec(
            slug="agenda",
            columns=[
                ColumnSpec(key="kind", type_id="enum", config={"options": ["appointment", "task"]}),
                ColumnSpec(key="title", type_id="text"),
                ColumnSpec(key="person", type_id="ref", config={"table": "person"}),
            ],
        ),
    ]
)
TO_NEW = Migration(
    desired=NEW,
    tables=[
        TableSource(target="person", source="contact", columns={"name": "full_name"}),
        TableSource(
            target="agenda",
            source="appointment",
            columns={"title": "title", "person": "with"},
            constants={"kind": "appointment"},
        ),
        TableSource(target="agenda", source="task", columns={"title": "title"}, constants={"kind": "task"}),
    ],
)


async def test_contacts_appointments_and_tasks_become_people_and_one_agenda(session: AsyncSession) -> None:
    old = Namespace(session, "ana/v1")
    await old.apply(OLD, ApplyOptions(index_builds=IndexBuilds.inline))
    ana = await (await old.table("contact")).create({"full_name": "Ana", "fax": "021 000"})
    await (await old.table("appointment")).create({"title": "dentist", "with": str(ana.id)})
    await (await old.table("task")).create({"title": "renew passport"})
    await session.commit()

    new = Namespace(session, "ana/v2")
    rehearsal = await old.migrate_to(new, TO_NEW, ApplyOptions(dry_run=True))
    assert {t.table: t.rows for t in rehearsal.tables} == {"person": 1, "agenda": 2}
    assert rehearsal.tables[0].columns_not_carried == {"contact": ("fax",)}
    assert await new.row_counts() == {}

    await old.migrate_to(new, TO_NEW)
    await session.commit()
    assert await new.row_counts() == {"agenda": 2, "person": 1}
    counted = await (await new.table("agenda")).count_by("kind")
    assert counted.groups == (ValueCount(value="appointment", count=1), ValueCount(value="task", count=1))
    person = (await (await new.table("person")).query()).items[0]
    appointment = [r for r in (await (await new.table("agenda")).query()).items if r["kind"] == "appointment"][0]
    assert appointment["person"] == str(person.id)

    await old.apply(Schema(), ApplyOptions(prune=True, allow_destructive=True, index_builds=IndexBuilds.inline))
    assert await old.row_counts() == {}
    assert await new.row_counts() == {"agenda": 2, "person": 1}
