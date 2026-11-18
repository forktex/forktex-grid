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

import sqlalchemy as sa
from _consumer import Report, check, engine, session_of
from forktex.error import AppError
from pydantic import ValidationError

from forktex_grid import (
    ApplyOptions,
    ColumnSpec,
    Namespace,
    NotFoundError,
    OffsetPaging,
    Query,
    Schema,
    TableSpec,
    apply_migrations,
    col,
    link,
)

NASTY = [
    "'; DROP TABLE sentinel; --",
    '" OR 1=1 --',
    "$$; DROP TABLE sentinel; $$",
    "robert'); DELETE FROM forktex_grid.grid_row; --",
    "%_\\",
    "ünïcødé 🙂 ✓",
    "‮evil",
    "a" * 255,
    "x\ny\tz",
]
INLINE = ApplyOptions(index_builds="inline")
SCHEMA = Schema.model_validate(
    {
        "tables": [
            {"slug": "client", "columns": [{"key": "name", "typeId": "text"}]},
            {
                "slug": "invoice",
                "columns": [
                    {"key": "memo", "typeId": "text"},
                    {"key": "client", "typeId": "ref", "config": {"table": "client"}},
                    {"key": "doc", "typeId": "json"},
                ],
            },
        ]
    }
)


async def refused(work: object) -> bool:
    try:
        await work
    except AppError, ValidationError:
        return True
    return False


async def main() -> None:
    report = Report()
    bind = engine()
    await apply_migrations(bind)
    async with bind.begin() as conn:
        await conn.execute(sa.text("CREATE TABLE sentinel (id int)"))
        await conn.execute(sa.text("INSERT INTO sentinel VALUES (1)"))

    async with session_of(bind) as s:
        for name in NASTY:
            ns = Namespace(s, name)
            await ns.apply(SCHEMA, INLINE)
            clients, invoices = await ns.table("client"), await ns.table("invoice")
            client = await clients.create({"name": name})
            await invoices.create({"memo": name, "client": client.id, "doc": {name: [name, {"k": name}]}})
            for op in ("eq", "contains", "startswith", "endswith", "icontains"):
                page = await invoices.query(
                    Query.model_validate({"where": {"column": "memo", "op": op, "value": name}})
                )
                check([r["memo"] for r in page.items] == [name], f"{op} on {name!r} returned {len(page.items)} rows")
            through = await invoices.query(
                Query(where=link("client").where(col("name") == name), select=(link("client").select("name"),))
            )
            check(len(through.items) == 1, f"link filter on {name!r}")
            report.count("nasty_namespaces")
        await s.commit()

    async with session_of(bind) as s:
        for slug in NASTY + ["client; drop table sentinel", "c" * 64, "", "with space"]:
            check(_slug_refused(slug), f"slug {slug!r} accepted")
            report.count("refused_slugs")
        for key in ["name; drop", "a b", "id", "1abc", "k" * 64, "naïve"]:
            check(_construct_refused(key), f"column key {key!r} accepted")
            report.count("refused_keys")
        for relation in ["public.client; drop table sentinel", "a.b.c", 'public."x"', ""]:
            check(_extends_refused(relation), f"extends {relation!r} accepted")
            report.count("refused_extends")
        await s.rollback()

    async with session_of(bind) as s:
        a, b = Namespace(s, "tenant-a"), Namespace(s, "tenant-b")
        for ns in (a, b):
            await ns.apply(SCHEMA, INLINE)
        a_client = await (await a.table("client")).create({"name": "secret-a"})
        a_invoice = await (await a.table("invoice")).create({"memo": "a-only", "client": a_client.id})
        b_invoices = await b.table("invoice")
        b_clients = await b.table("client")
        check(await refused(b_invoices.create({"memo": "steal", "client": a_client.id})), "B linked to A's client")
        for attempt in (
            b_clients.get(a_client.id),
            b_invoices.get(a_invoice.id),
            b_invoices.patch(a_invoice.id, values={"memo": "pwned"}),
            b_invoices.archive(a_invoice.id),
        ):
            try:
                await attempt
                check(False, "B touched A's row")
            except NotFoundError:
                report.count("cross_tenant_refused")
        everything_b = await b_invoices.query(Query(page=OffsetPaging(size=1000), count=True))
        check(everything_b.total == 0, "B sees rows")
        by_id = await b_invoices.query(Query(where=col("id") == a_invoice.id))
        check(by_id.items == [], "B found A's row by id")
        await b_invoices.upsert_by_external_ref("shared-key", values={"memo": "b"})
        await (await a.table("invoice")).upsert_by_external_ref("shared-key", values={"memo": "a"})
        b_shared = await b_invoices.get_by_external_ref("shared-key")
        check(b_shared is not None and b_shared["memo"] == "b", "host keys crossed namespaces")
        a_after = await (await a.table("invoice")).get(a_invoice.id)
        check(a_after["memo"] == "a-only", "A's row changed")
        await s.commit()

    async with bind.connect() as conn:
        check((await conn.execute(sa.text("SELECT count(*) FROM sentinel"))).scalar_one() == 1, "sentinel lost")
        rows = (await conn.execute(sa.text("SELECT count(*) FROM forktex_grid.grid_row"))).scalar_one()
        check(rows >= 2 * len(NASTY), f"grid rows went missing: {rows}")
    report.emit()
    await bind.dispose()


def _construct_refused(key: str) -> bool:
    try:
        ColumnSpec(key=key, type_id="text")
    except AppError, ValidationError:
        return True
    return False


def _slug_refused(slug: str) -> bool:
    try:
        TableSpec(slug=slug)
    except AppError, ValidationError:
        return True
    return False


def _extends_refused(relation: str) -> bool:
    try:
        TableSpec(slug="x", extends=relation)
    except AppError, ValidationError:
        return True
    return False


if __name__ == "__main__":
    asyncio.run(main())
