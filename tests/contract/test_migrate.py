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

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from forktex_grid import (
    ApplyOptions,
    AlreadyExistsError,
    BadRequestError,
    Cardinality,
    ColumnSpec,
    Migration,
    Namespace,
    Query,
    Schema,
    TableSource,
    TableSpec,
    col,
)
from tests.conftest import declare

CONTACT = TableSpec(
    slug="contact",
    columns=(
        ColumnSpec(key="full_name", type_id="text", is_required=True),
        ColumnSpec(key="phone", type_id="text"),
        ColumnSpec(key="fax", type_id="text"),
    ),
)
APPOINTMENT = TableSpec(
    slug="appointment",
    columns=(
        ColumnSpec(key="title", type_id="text"),
        ColumnSpec(key="with_contact", type_id="ref", config={"table": "contact"}),
    ),
)
TASK = TableSpec(
    slug="task",
    columns=(
        ColumnSpec(key="title", type_id="text"),
        ColumnSpec(key="for_contacts", type_id="ref", cardinality=Cardinality.many, config={"table": "contact"}),
    ),
)
NOTE = TableSpec(slug="note", columns=(ColumnSpec(key="text", type_id="text"),))

LIFE = Schema(
    tables=(
        TableSpec(
            slug="person",
            columns=(
                ColumnSpec(key="name", type_id="text", is_required=True),
                ColumnSpec(key="phone", type_id="text"),
                ColumnSpec(key="was_ref", type_id="text"),
            ),
        ),
        TableSpec(
            slug="agenda",
            columns=(
                ColumnSpec(key="kind", type_id="enum", is_required=True, config={"options": ["appointment", "task"]}),
                ColumnSpec(key="title", type_id="text"),
                ColumnSpec(key="people", type_id="ref", cardinality=Cardinality.many, config={"table": "person"}),
            ),
        ),
    )
)
TO_LIFE = Migration(
    desired=LIFE,
    tables=(
        TableSource(
            target="person",
            source="contact",
            columns={"name": "full_name", "phone": "phone", "was_ref": "external_ref"},
        ),
        TableSource(
            target="agenda", source="appointment", columns={"title": "title"}, constants={"kind": "appointment"}
        ),
        TableSource(
            target="agenda",
            source="task",
            columns={"title": "title", "people": "for_contacts"},
            constants={"kind": "task"},
        ),
    ),
)


async def _personal(session: AsyncSession) -> Namespace:
    ns = Namespace(session, "personal")
    contacts, appointments, tasks, notes = await declare(ns, CONTACT, APPOINTMENT, TASK, NOTE)
    ana = await contacts.create({"full_name": "Ana", "phone": "0721", "fax": "f"}, external_ref="c-ana")
    bob = await contacts.create({"full_name": "Bob"})
    await appointments.create({"title": "dentist", "with_contact": str(ana.id)})
    await tasks.create({"title": "call both", "for_contacts": [str(ana.id), str(bob.id)]})
    await notes.create({"text": "left behind"})
    return ns


async def test_a_namespace_moves_into_a_new_shape_with_its_links_repointed(session: AsyncSession) -> None:
    personal = await _personal(session)
    life = Namespace(session, "life")
    report = await personal.migrate_to(life, TO_LIFE)

    assert {t.table: t.rows for t in report.tables} == {"person": 2, "agenda": 2}
    assert {t.table: t.sources for t in report.tables} == {"person": ("contact",), "agenda": ("appointment", "task")}
    person_report = next(t for t in report.tables if t.table == "person")
    assert person_report.columns_not_carried == {"contact": ("fax",)}
    agenda_report = next(t for t in report.tables if t.table == "agenda")
    assert agenda_report.columns_not_carried == {"appointment": ("with_contact",)}
    assert report.tables_not_carried == ("note",)
    assert report.dry_run is False and report.schema_report.plan.changes

    people = await life.table("person")
    ana = (await people.query(Query(where=col("name") == "Ana"))).items[0]
    assert ana.external_ref == "c-ana" and ana["was_ref"] == "c-ana" and ana["phone"] == "0721"
    agenda = await life.table("agenda")
    task = (await agenda.query(Query(where=col("kind") == "task"))).items[0]
    assert set(task["people"]) == {str(r.id) for r in (await people.query()).items}
    assert await (await personal.table("contact")).count() == 2


async def test_a_copied_row_keeps_when_it_was_created_and_changed(session: AsyncSession) -> None:
    personal = await _personal(session)
    source = (await (await personal.table("contact")).query(Query(where=col("full_name") == "Ana"))).items[0]
    life = Namespace(session, "life")
    await personal.migrate_to(life, TO_LIFE)
    copied = (await (await life.table("person")).query(Query(where=col("name") == "Ana"))).items[0]
    assert (copied.created_at, copied.updated_at) == (source.created_at, source.updated_at)
    assert copied.id != source.id


async def test_a_dry_run_reports_the_same_and_keeps_nothing(session: AsyncSession) -> None:
    personal = await _personal(session)
    life = Namespace(session, "life")
    rehearsed = await personal.migrate_to(life, TO_LIFE, ApplyOptions(dry_run=True))
    assert rehearsed.dry_run is True and rehearsed.schema_report.options.dry_run is True
    assert (await life.describe()).tables == ()
    real = await personal.migrate_to(life, TO_LIFE)
    assert [t.model_dump() for t in real.tables] == [t.model_dump() for t in rehearsed.tables]


async def test_without_a_column_map_every_shared_key_is_copied(session: AsyncSession) -> None:
    personal = await _personal(session)
    archive = Namespace(session, "archive")
    slim = CONTACT.model_copy(update={"columns": CONTACT.columns[:2]})
    report = await personal.migrate_to(
        archive, Migration(desired=Schema(tables=(slim,)), tables=(TableSource(target="contact", source="contact"),))
    )
    assert report.tables[0].columns_not_carried == {"contact": ("fax",)}
    names = {r["full_name"] for r in (await (await archive.table("contact")).query()).items}
    assert names == {"Ana", "Bob"}


async def test_links_inside_a_table_and_across_a_cycle_are_set_once_every_row_exists(session: AsyncSession) -> None:
    source = Namespace(session, "org")
    team = TableSpec(
        slug="team",
        columns=(
            ColumnSpec(key="name", type_id="text"),
            ColumnSpec(key="parent", type_id="ref", config={"table": "team"}),
            ColumnSpec(key="lead", type_id="ref", config={"table": "member"}),
        ),
    )
    member = TableSpec(
        slug="member",
        columns=(
            ColumnSpec(key="name", type_id="text"),
            ColumnSpec(key="team", type_id="ref", config={"table": "team"}),
        ),
    )
    teams, members = await declare(source, team, member)
    root = await teams.create({"name": "root"})
    child = await teams.create({"name": "child", "parent": str(root.id)})
    alice = await members.create({"name": "alice", "team": str(child.id)})
    await teams.patch(child.id, values={"lead": str(alice.id)})

    copy = Namespace(session, "org2")
    report = await source.migrate_to(
        copy,
        Migration(
            desired=Schema(tables=(team, member)),
            tables=(TableSource(target="team", source="team"), TableSource(target="member", source="member")),
        ),
    )
    assert all(t.links_dropped == 0 for t in report.tables)
    new_teams = {r["name"]: r for r in (await (await copy.table("team")).query()).items}
    new_alice = (await (await copy.table("member")).query()).items[0]
    assert new_teams["child"]["parent"] == str(new_teams["root"].id)
    assert new_teams["child"]["lead"] == str(new_alice.id)
    assert new_alice["team"] == str(new_teams["child"].id)


async def test_a_required_link_inside_a_cycle_is_refused_with_the_way_out(session: AsyncSession) -> None:
    source = Namespace(session, "loop")
    node = TableSpec(slug="node", columns=(ColumnSpec(key="next", type_id="ref", config={"table": "node"}),))
    (nodes,) = await declare(source, node)
    first = await nodes.create({})
    second = await nodes.create({"next": str(first.id)})
    await nodes.patch(first.id, values={"next": str(second.id)})
    strict = node.model_copy(
        update={"columns": (ColumnSpec(key="next", type_id="ref", is_required=True, config={"table": "node"}),)}
    )
    with pytest.raises(BadRequestError, match="'node.next' is a required link inside a cycle"):
        await source.migrate_to(
            Namespace(session, "loop2"),
            Migration(desired=Schema(tables=(strict,)), tables=(TableSource(target="node", source="node"),)),
        )


async def test_a_link_to_a_table_left_behind_is_dropped_and_counted(session: AsyncSession) -> None:
    personal = await _personal(session)
    only_tasks = Migration(desired=Schema(tables=(TASK,)), tables=(TableSource(target="task", source="task"),))
    with pytest.raises(BadRequestError, match="'contact'"):
        await personal.migrate_to(Namespace(session, "tasks"), only_tasks)
    tasks_only = TASK.model_copy(
        update={
            "columns": (*TASK.columns[:1], ColumnSpec(key="for_contacts", type_id="text", cardinality=Cardinality.many))
        }
    )
    report = await personal.migrate_to(
        Namespace(session, "tasks"),
        Migration(desired=Schema(tables=(tasks_only,)), tables=(TableSource(target="task", source="task"),)),
    )
    assert report.tables[0].rows == 1


async def test_a_value_that_does_not_fit_its_new_type_names_its_row(session: AsyncSession) -> None:
    personal = await _personal(session)
    numbers = Schema(tables=(TableSpec(slug="n", columns=(ColumnSpec(key="v", type_id="integer"),)),))
    mapping = Migration(
        desired=numbers, tables=(TableSource(target="n", source="contact", columns={"v": "full_name"}),)
    )
    with pytest.raises(BadRequestError, match=r"row .* of 'contact' does not fit 'n': column 'v'"):
        await personal.migrate_to(Namespace(session, "numbers"), mapping)


async def test_refuses_what_a_migration_cannot_be(session: AsyncSession, grid_engine: AsyncEngine) -> None:
    personal = await _personal(session)
    with pytest.raises(BadRequestError, match="into another namespace"):
        await personal.migrate_to(personal, TO_LIFE)
    busy = Namespace(session, "busy")
    await declare(busy, NOTE)
    with pytest.raises(BadRequestError, match="already has tables"):
        await personal.migrate_to(busy, TO_LIFE)
    with pytest.raises(BadRequestError, match="has no table"):
        await personal.migrate_to(
            Namespace(session, "x"),
            Migration(desired=Schema(tables=(NOTE,)), tables=(TableSource(target="note", source="nope"),)),
        )
    with pytest.raises(BadRequestError, match="no column"):
        await personal.migrate_to(
            Namespace(session, "x"),
            Migration(
                desired=Schema(tables=(NOTE,)),
                tables=(TableSource(target="note", source="note", columns={"text": "body"}),),
            ),
        )
    with pytest.raises(BadRequestError, match="filled but not declared"):
        Migration(desired=Schema(tables=(NOTE,)), tables=(TableSource(target="other", source="note"),))
    async with async_sessionmaker(bind=grid_engine)() as other:
        with pytest.raises(BadRequestError, match="same session"):
            await personal.migrate_to(Namespace(other, "life"), TO_LIFE)


async def test_merging_two_tables_onto_one_unique_value_is_refused_whole(session: AsyncSession) -> None:
    source = Namespace(session, "two-lists")
    left, right = await declare(source, NOTE, NOTE.model_copy(update={"slug": "memo"}))
    await left.create({"text": "same"})
    await right.create({"text": "same"})
    unique = TableSpec(slug="note", columns=(ColumnSpec(key="text", type_id="text", is_unique=True),))
    merge = Migration(
        desired=Schema(tables=(unique,)),
        tables=(TableSource(target="note", source="note"), TableSource(target="note", source="memo")),
    )
    target = Namespace(session, "one-list")
    with pytest.raises(AlreadyExistsError, match="two copied rows hold the same unique value"):
        await source.migrate_to(target, merge)
    assert (await target.describe()).tables == ()


async def test_a_table_that_extends_a_host_needs_every_rows_host_key(session: AsyncSession) -> None:
    source = await _personal(session)
    extension = TableSpec(slug="person", extends="public.client", columns=(ColumnSpec(key="name", type_id="text"),))
    mapping = Migration(
        desired=Schema(tables=(extension,)),
        tables=(TableSource(target="person", source="contact", columns={"name": "full_name"}),),
    )
    with pytest.raises(BadRequestError, match="has no external_ref, and 'person' extends public.client"):
        await source.migrate_to(Namespace(session, "ext"), mapping)


async def test_a_value_moves_between_a_single_and_a_list_column(session: AsyncSession) -> None:
    source = await _personal(session)
    phones = TableSpec(
        slug="contact",
        columns=(
            ColumnSpec(key="full_name", type_id="text", cardinality=Cardinality.many),
            ColumnSpec(key="phone", type_id="text", cardinality=Cardinality.many),
        ),
    )
    listed = Namespace(session, "listed")
    await source.migrate_to(
        listed, Migration(desired=Schema(tables=(phones,)), tables=(TableSource(target="contact", source="contact"),))
    )
    rows = {tuple(r["full_name"]): r.get("phone") for r in (await (await listed.table("contact")).query()).items}
    assert rows == {("Ana",): ["0721"], ("Bob",): None}

    back = Namespace(session, "back")
    await listed.migrate_to(
        back, Migration(desired=Schema(tables=(CONTACT,)), tables=(TableSource(target="contact", source="contact"),))
    )
    assert {r["full_name"] for r in (await (await back.table("contact")).query()).items} == {"Ana", "Bob"}

    await (await listed.table("contact")).create({"full_name": ["Cy", "Dee"]})
    with pytest.raises(BadRequestError, match="holds several values for 'contact.full_name', which holds one"):
        await listed.migrate_to(
            Namespace(session, "again"),
            Migration(desired=Schema(tables=(CONTACT,)), tables=(TableSource(target="contact", source="contact"),)),
        )


async def test_two_migrations_into_one_namespace_cannot_both_find_it_empty(grid_engine: AsyncEngine) -> None:
    maker = async_sessionmaker(bind=grid_engine, expire_on_commit=False)
    async with maker() as setup:
        await _personal(setup)
        await setup.commit()
    async with maker() as first, maker() as second:
        await Namespace(first, "personal").migrate_to(Namespace(first, "life"), TO_LIFE)
        racing = asyncio.create_task(Namespace(second, "personal").migrate_to(Namespace(second, "life"), TO_LIFE))
        await asyncio.sleep(0.3)
        assert not racing.done()
        await first.commit()
        with pytest.raises(BadRequestError, match="already has tables"):
            await racing
        await second.rollback()
    async with maker() as check:
        assert await Namespace(check, "life").row_counts() == {"agenda": 2, "person": 2}


async def test_json_lists_are_copied_as_they_are(session: AsyncSession) -> None:
    source = Namespace(session, "jsons")
    spec = TableSpec(slug="t", columns=(ColumnSpec(key="extra", type_id="json"),))
    (table,) = await declare(source, spec)
    for value in (["x"], [], [1, 2], {"k": [1]}):
        await table.create({"extra": value})
    copy = Namespace(session, "jsons2")
    await source.migrate_to(
        copy, Migration(desired=Schema(tables=(spec,)), tables=(TableSource(target="t", source="t"),))
    )
    copied = sorted(str(r.get("extra")) for r in (await (await copy.table("t")).query()).items)
    assert copied == sorted(str(v) for v in (["x"], [], [1, 2], {"k": [1]}))


async def test_a_required_link_that_only_depends_on_a_cycle_is_filled_after_it(session: AsyncSession) -> None:
    source = Namespace(session, "cyc")
    a = TableSpec(slug="a", columns=(ColumnSpec(key="b", type_id="ref", config={"table": "b"}),))
    b = TableSpec(slug="b", columns=(ColumnSpec(key="a", type_id="ref", config={"table": "a"}),))
    z = TableSpec(slug="z", columns=(ColumnSpec(key="a", type_id="ref", is_required=True, config={"table": "a"}),))
    c = TableSpec(slug="c", columns=(ColumnSpec(key="z", type_id="ref", is_required=True, config={"table": "z"}),))
    ta, tb, tz, tc = await declare(source, a, b, z, c)
    ra = await ta.create({})
    rb = await tb.create({"a": str(ra.id)})
    await ta.patch(ra.id, values={"b": str(rb.id)})
    rz = await tz.create({"a": str(ra.id)})
    await tc.create({"z": str(rz.id)})
    every = tuple(TableSource(target=s, source=s) for s in ("a", "b", "z", "c"))
    copy = Namespace(session, "cyc2")
    report = await source.migrate_to(copy, Migration(desired=Schema(tables=(a, b, z, c)), tables=every))
    assert {t.table: t.rows for t in report.tables} == {"a": 1, "b": 1, "z": 1, "c": 1}
    new_a = (await (await copy.table("a")).query()).items[0]
    new_b = (await (await copy.table("b")).query()).items[0]
    assert new_a["b"] == str(new_b.id) and new_b["a"] == str(new_a.id)
