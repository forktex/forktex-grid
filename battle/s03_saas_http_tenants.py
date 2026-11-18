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
import os
import socket
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import httpx
import uvicorn
from _consumer import Report, check, engine, rng
from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from forktex.error import AppError, AppErrorCode
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forktex_grid import ApplyOptions, Batch, Namespace, Page, Query, Row, Schema, apply_migrations

TENANTS = int(os.environ.get("BATTLE_TENANTS", "25"))
WORKERS_PER_TENANT = 4
OPS_PER_WORKER = int(os.environ.get("BATTLE_OPS", "60"))
STATUS = {
    AppErrorCode.NOT_FOUND: 404,
    AppErrorCode.BAD_REQUEST: 400,
    AppErrorCode.ALREADY_EXISTS: 409,
    AppErrorCode.CONFLICT: 409,
}

bind = engine(pool_size=40)
maker = async_sessionmaker(bind=bind, expire_on_commit=False)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await apply_migrations(bind)
    yield
    await bind.dispose()


app = FastAPI(lifespan=lifespan)


@app.exception_handler(AppError)
async def grid_error(_: Request, exc: AppError) -> JSONResponse:
    return JSONResponse({"code": exc.code, "message": str(exc)}, status_code=STATUS.get(exc.code, 500))


async def transaction() -> AsyncIterator[AsyncSession]:
    async with maker() as s:
        yield s
        await s.commit()


async def tenant_grid(
    session: Annotated[AsyncSession, Depends(transaction)], x_tenant: Annotated[str, Header()]
) -> Namespace:
    return Namespace(session, f"tenant:{x_tenant}")


Grid = Annotated[Namespace, Depends(tenant_grid)]


@app.put("/schema")
async def put_schema(grid: Grid, desired: Schema) -> dict[str, Any]:
    report = await grid.apply(desired, ApplyOptions(index_builds="inline"))
    return {"changes": len(report.plan.changes)}


@app.post("/batch")
async def post_batch(grid: Grid, batch: Batch) -> dict[str, int]:
    return {"written": len((await grid.batch(batch)).results)}


@app.post("/tables/{slug}/rows")
async def create_row(grid: Grid, slug: str, values: dict[str, Any]) -> Row:
    return await (await grid.table(slug)).create(values)


@app.get("/tables/{slug}/rows/{row_id}")
async def get_row(grid: Grid, slug: str, row_id: uuid.UUID) -> Row:
    return await (await grid.table(slug)).get(row_id)


@app.patch("/tables/{slug}/rows/{row_id}")
async def patch_row(grid: Grid, slug: str, row_id: uuid.UUID, values: dict[str, Any]) -> Row:
    return await (await grid.table(slug)).patch(row_id, values=values)


@app.delete("/tables/{slug}/rows/{row_id}", status_code=204)
async def archive_row(grid: Grid, slug: str, row_id: uuid.UUID) -> None:
    await (await grid.table(slug)).archive(row_id)


@app.post("/tables/{slug}/query")
async def query_rows(grid: Grid, slug: str, query: Query) -> Page:
    return await (await grid.table(slug)).query(query)


SCHEMA = {
    "tables": [
        {
            "slug": "customer",
            "columns": [
                {"key": "email", "typeId": "text", "isUnique": True},
                {"key": "tier", "typeId": "enum", "config": {"options": ["free", "pro"]}},
            ],
        },
        {
            "slug": "order",
            "columns": [
                {"key": "ref", "typeId": "text", "isRequired": True},
                {"key": "amount", "typeId": "decimal"},
                {"key": "customer", "typeId": "ref", "config": {"table": "customer", "onDelete": "cascade"}},
            ],
        },
    ]
}


class Ledger:
    def __init__(self) -> None:
        self.created: dict[str, set[str]] = {}
        self.archived: dict[str, set[str]] = {}
        self.statuses: dict[int, int] = {}
        self.leaks: list[str] = []
        self.conflicts: dict[str, int] = {}


async def tenant_worker(client: httpx.AsyncClient, tenant: str, worker: int, ledger: Ledger) -> None:
    r = rng(f"{tenant}:{worker}")
    headers = {"X-Tenant": tenant}
    mine: list[str] = []
    customers: list[str] = []

    async def call(method: str, url: str, **kw: Any) -> httpx.Response:
        response = await client.request(method, url, headers=headers, **kw)
        ledger.statuses[response.status_code] = ledger.statuses.get(response.status_code, 0) + 1
        if response.status_code == 409:
            code = response.json().get("code", "?")
            ledger.conflicts[code] = ledger.conflicts.get(code, 0) + 1
        return response

    for _ in range(OPS_PER_WORKER):
        roll = r.random()
        if roll < 0.2:
            response = await call(
                "POST",
                "/tables/customer/rows",
                json={"email": f"{tenant}-{r.randint(0, 30)}@x.io", "tier": r.choice(["free", "pro"])},
            )
            if response.status_code == 200:
                customers.append(response.json()["id"])
                ledger.created.setdefault(tenant, set()).add(response.json()["id"])
        elif roll < 0.55:
            body = {
                "ref": f"{tenant}/{uuid.uuid4().hex[:6]}",
                "amount": f"{r.randint(1, 999)}.50",
                "customer": r.choice(customers) if customers else None,
            }
            response = await call("POST", "/tables/order/rows", json=body)
            if response.status_code == 200:
                mine.append(response.json()["id"])
                ledger.created.setdefault(tenant, set()).add(response.json()["id"])
        elif roll < 0.65 and mine:
            await call("PATCH", f"/tables/order/rows/{r.choice(mine)}", json={"amount": "1.00"})
        elif roll < 0.72 and mine:
            target = mine.pop(r.randrange(len(mine)))
            response = await call("DELETE", f"/tables/order/rows/{target}")
            if response.status_code == 204:
                ledger.archived.setdefault(tenant, set()).add(target)
        elif roll < 0.9:
            query = {
                "where": {"link": "customer", "where": {"column": "tier", "op": "eq", "value": "pro"}},
                "select": ["ref", {"link": "customer", "select": ["email"]}],
                "count": True,
                "page": {"size": 200},
            }
            response = await call("POST", "/tables/order/query", json=query)
            for item in response.json().get("items", []):
                if not str(item["values"]["ref"]).startswith(f"{tenant}/"):
                    ledger.leaks.append(f"{tenant} saw {item['values']['ref']}")
                embedded = item["links"].get("customer")
                if embedded and not str(embedded["values"]["email"]).startswith(f"{tenant}-"):
                    ledger.leaks.append(f"{tenant} saw customer {embedded['values']['email']}")
        else:
            garbage = r.choice(
                [
                    {"where": {"column": "amount", "op": "gte", "value": "lots"}},
                    {"where": {"link": "ref", "where": {"column": "x", "op": "eq", "value": 1}}},
                    {"where": {"column": "nope", "op": "eq", "value": 1}},
                    {"page": {"size": 0}},
                    {"select": [{"link": "customer", "select": ["nope"]}]},
                    {"orderBy": [{"column": "amount", "direction": "sideways"}]},
                    {"where": {"and": []}},
                    {"bogus": True},
                ]
            )
            response = await call("POST", "/tables/order/query", json=garbage)
            check(response.status_code in (400, 422), f"garbage query answered {response.status_code}: {garbage}")


async def attacker(client: httpx.AsyncClient, ledger: Ledger) -> None:
    for victim, ids in ledger.created.items():
        for row_id in list(ids)[:10]:
            for table in ("order", "customer"):
                response = await client.get(f"/tables/{table}/rows/{row_id}", headers={"X-Tenant": f"not-{victim}"})
                if response.status_code != 404:
                    ledger.leaks.append(f"attacker read {victim}'s {table} {row_id}: {response.status_code}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def main() -> None:
    report = Report()
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    ledger = Ledger()
    limits = httpx.Limits(max_connections=200)
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=60, limits=limits) as client:
        tenants = [f"t{i:02d}" for i in range(TENANTS)]
        for tenant in tenants:
            response = await client.put("/schema", json=SCHEMA, headers={"X-Tenant": tenant})
            check(response.status_code == 200, f"schema for {tenant}: {response.status_code} {response.text}")
        await asyncio.gather(*(tenant_worker(client, t, w, ledger) for t in tenants for w in range(WORKERS_PER_TENANT)))
        await attacker(client, ledger)
        for tenant in tenants:
            response = await client.post(
                "/tables/order/query", json={"count": True, "page": {"size": 1}}, headers={"X-Tenant": tenant}
            )
            orders_created = {i for i in ledger.created.get(tenant, set())}
            listed = await client.post(
                "/tables/order/query", json={"select": [], "page": {"size": 1000}}, headers={"X-Tenant": tenant}
            )
            live_orders = {item["id"] for item in listed.json()["items"]}
            check(live_orders <= orders_created, f"{tenant} lists orders it never created")
            check(not (live_orders & ledger.archived.get(tenant, set())), f"{tenant} lists archived orders")
            check(
                response.json()["total"] == len(live_orders),
                f"{tenant}: count {response.json()['total']} vs listed {len(live_orders)}",
            )
    server.should_exit = True
    await serving
    check(not ledger.leaks, f"cross-tenant leaks: {ledger.leaks[:5]}")
    check(not [s for s in ledger.statuses if s >= 500], f"server errors: {ledger.statuses}")
    report.set("tenants", TENANTS)
    report.set("statuses", dict(sorted(ledger.statuses.items())))
    report.set("requests", sum(ledger.statuses.values()))
    report.set("conflicts_by_code", ledger.conflicts)
    report.emit()


if __name__ == "__main__":
    asyncio.run(main())
