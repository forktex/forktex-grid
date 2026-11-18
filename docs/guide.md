# Guide

Most chapters are one complete file. Copy it, point `DATABASE_URL` at a
PostgreSQL 14+ database, and run it:

```bash
pip install forktex-grid
export DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/postgres
python first_table.py
```

1. [Your first table](#your-first-table)
2. [Run inside a web app](#run-inside-a-web-app)
3. [Add tenant fields to a table you already own](#add-tenant-fields-to-a-table-you-already-own)
4. [Ask questions: filters, links, embeds, pages](#ask-questions-filters-links-embeds-pages)
5. [Read values as Python and validate forms](#read-values-as-python-and-validate-forms)
6. [Change a schema](#change-a-schema)
7. [Link rows](#link-rows)
8. [Write a schema and its rows in one transaction](#write-a-schema-and-its-rows-in-one-transaction)
9. [Add your own field type](#add-your-own-field-type)
10. [Give an agent its own tables](#give-an-agent-its-own-tables)
11. [Reshape a namespace](#reshape-a-namespace)

The ideas behind them are in [concepts](concepts.md); every name is in the
[reference](reference.md). Each chapter ends with what proves it: the tests
that run on every build, and the battle scenario that holds it under load,
attack or failure.

## Your first table

Declare a table as data, write rows, ask for them.

```python
# first_table.py
import asyncio
import os

from forktex.database import close_engine, get_engine, init_engine, session, transaction

from forktex_grid import ColumnSpec, Namespace, Query, Schema, TableSpec, apply_migrations, col

SCHEMA = Schema(
    tables=[
        TableSpec(
            slug="invoice",
            columns=[
                ColumnSpec(key="number", type_id="text", is_required=True, is_unique=True),
                ColumnSpec(key="total", type_id="decimal"),
                ColumnSpec(key="issued_on", type_id="date"),
            ],
        )
    ]
)


async def main() -> None:
    await init_engine(os.environ["DATABASE_URL"])
    await apply_migrations(get_engine())  # grid's own tables, in the forktex_grid schema

    async with session() as s:  # apply commits: hand it a session(), not a transaction()
        report = await Namespace(s, "acme").apply(SCHEMA)
        assert report.complete

    async with transaction() as s:
        invoices = await Namespace(s, "acme").table("invoice")
        await invoices.create({"number": "INV-001", "total": "250.00", "issued_on": "2026-03-01"})
        await invoices.create({"number": "INV-002", "total": "80.50", "issued_on": "2026-03-04"})

        page = await invoices.query(Query(where=col("total") >= 100, order_by=(col("issued_on").desc(),)))
        for row in page.items:
            print(row["number"], row["total"])  # INV-001 250.00

    await close_engine()


asyncio.run(main())
```

`apply` is idempotent: applying the same schema again changes nothing. A unique
column is a real Postgres unique index, so a second `INV-001` in the same
namespace is refused with `AlreadyExistsError` — which is also what running
this file a second time against the same database does.

*Proven by* `tests/usage/test_first_table.py`; replicas starting together: `battle/s01`.

## Run inside a web app

One `Namespace` per request, built from the tenant your authentication
resolved. Row work joins the request's transaction. A `Query` is a pydantic
model, so a route takes it as its body as is.

```python
# app.py
import os
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header
from sqlalchemy.ext.asyncio import AsyncSession

from forktex.api import AppConfig, create_app
from forktex.database import close_engine, get_engine, init_engine, transaction

from forktex_grid import Namespace, Page, Query, apply_migrations


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_engine(os.environ["DATABASE_URL"])
    await apply_migrations(get_engine())  # idempotent, safe on every replica at once
    yield
    await close_engine()


app = create_app(AppConfig(title="Invoices", lifespan=lifespan))
Session = Annotated[AsyncSession, Depends(transaction.dependency, scope="function")]


def tenant(x_tenant: Annotated[str, Header()]) -> str:
    return x_tenant  # stands in for your authentication: never trust a header in production


async def grid(session: Session, tenant_id: Annotated[str, Depends(tenant)]) -> Namespace:
    return Namespace(session, tenant_id)


Grid = Annotated[Namespace, Depends(grid)]


@app.post("/tables/{slug}/query")
async def query_rows(ns: Grid, slug: str, query: Query) -> Page:
    return await (await ns.table(slug)).query(query)
```

```bash
uvicorn app:app
```

Grid's errors derive from `forktex.error.AppError`, so the app factory renders
`NotFoundError` as a 404 and `BadRequestError` as a 400 with the usual envelope.
The route's OpenAPI document carries the whole query language.

Make the namespace come from one place. A route that takes it as a parameter is
a second way to name a tenant, and the first one to get it wrong reads another
tenant's rows.

*Proven by* `battle/s03`: 25 tenants over HTTP, 100 clients, an attacker and a
body fuzzer.

## Add tenant fields to a table you already own

Your `client` table is yours: typed, constrained, migrated. A tenant that wants
its own fields on it gets a grid table that `extends` it: each of its rows adds
fields to one of your rows, keyed by that row's primary key (its
`external_ref`). Your table is never read or altered.

```python
# client_fields.py
import asyncio
import os
import uuid

import sqlalchemy as sa
from forktex.database import close_engine, get_engine, init_engine, session, transaction

from forktex_grid import ColumnSpec, Namespace, Query, Schema, TableSpec, apply_migrations, col

CLIENT_FIELDS = TableSpec(
    slug="client_fields",
    label="Client",
    extends="public.client",
    columns=[
        ColumnSpec(key="account_manager", label="Account manager", type_id="text", display_order=1),
        ColumnSpec(key="tier", type_id="enum", config={"options": ["gold", "silver"]}, display_order=2),
        ColumnSpec(key="signed_mandate", label="Mandate signed", type_id="boolean", default_value=False),
    ],
)


async def main() -> None:
    await init_engine(os.environ["DATABASE_URL"])
    await apply_migrations(get_engine())
    async with get_engine().begin() as conn:  # your own table, from your own migrations
        await conn.execute(sa.text("CREATE TABLE IF NOT EXISTS public.client (id uuid PRIMARY KEY, name text)"))

    async with session() as s:
        await Namespace(s, "acme").apply(Schema(tables=[CLIENT_FIELDS]))

    ada, bob = uuid.uuid7(), uuid.uuid7()
    async with transaction() as s:
        fields = await Namespace(s, "acme").table("client_fields")

        # Create-or-update by your row's key: what a "save" button needs.
        await fields.upsert_by_external_ref(ada, values={"account_manager": "IP", "tier": "gold"})
        await fields.upsert_by_external_ref(ada, values={"signed_mandate": True})

        one = await fields.get_by_external_ref(ada)
        print(one["account_manager"], one["tier"], one["signed_mandate"])  # IP gold True

        # A list screen: one query for the whole page of your rows.
        # bob has nothing recorded yet, so only ada's key comes back.
        page = await fields.get_many_by_external_ref([ada, bob])
        print(sorted(page) == [str(ada)])  # True

        # Which of your clients are gold? Their keys, to filter your own query by.
        gold = await fields.query(Query(where=col("tier") == "gold", select=()))
        print([r.external_ref for r in gold.items] == [str(ada)])  # True

    await close_engine()


asyncio.run(main())
```

The host key is stored as text, so a `bigserial` or `varchar` key works as well
as a `uuid`. Every row of a table that `extends` another carries one: `create`
without `external_ref` is refused. Values are typed by their column: `tier`
refuses anything not in its options, `signed_mandate` anything that is not a
boolean.

To render the fields on a screen, read the table's spec — `fields.spec.columns`
lists every column with its label, type and display order — or its model,
below: a field a tenant adds appears without a frontend release.

*Proven by* `tests/usage/test_tenant_fields.py`; a 20 000-row host table and racing upserts: `battle/s04`.

## Ask questions: filters, links, embeds, pages

A `Query` is the one way to read many rows. Its parts compose freely:

```python
from forktex_grid import CursorPaging, Query, col, link

# Sent invoices over 100 of Romanian clients, biggest first, with the client's name.
Query(
    where=(col("status") == "sent") & (col("total") > 100) & link("client").where(col("country") == "RO"),
    order_by=(col("total").desc(),),
    select=("number", "total", link("client").select("name")),
    count=True,
)

# Every invoice tagged "late", walked 500 at a time.
Query(where=link("tags").some(col("name") == "late"), page=CursorPaging(size=500))

# Created this month, by the row's own timestamp.
Query(where=col("created_at") >= month_start)

# How many sent invoices per currency: counted in Postgres, not from a page.
await invoices.count_by("currency", col("status") == "sent")
```

An agent writes the same queries as JSON; `Query.model_validate(document)` reads
them. Operators per type, nulls, link matching, embeds and paging are all in
[the query language](query.md).

*Proven by* `tests/usage/test_linked_records.py`, `tests/contract/test_query_*.py` and `tests/properties/test_query_oracle.py`; against a model of the data: `battle/s02`; at scale: `battle/s07`.

## Read values as Python and validate forms

Rows are JSON: a `decimal` arrives as `"250.00"`, a `date` as `"2026-11-01"`.
Each table also has a pydantic model built from its columns, which turns a row
into Python values and validates a form before it is written.

```python
invoices = await ns.table("invoice")
model = invoices.model()                 # a pydantic model: one field per column

row = await invoices.get(invoice_id)
typed = invoices.typed(row)              # model.model_validate(row.values)
typed.total                              # Decimal("250.00")
typed.issued_on                          # datetime.date(2026, 3, 1)

form = model.model_validate(request_json)          # refuses what the table would refuse
await invoices.create(form.model_dump(mode="json", exclude_none=True))

model.model_json_schema()                # labels as titles, enum options, required columns
```

| Field type | Python |
|---|---|
| `text` | `str` (with its `max_length`) |
| `integer` | `int` |
| `decimal` | `Decimal` (with its precision and scale) |
| `boolean` | `bool` |
| `date`, `datetime` | `date`, `datetime` |
| `uuid`, `ref` | `UUID` |
| `enum` | `Literal[...]` of its options |
| `json` | any JSON value |
| `many` cardinality | `list[...]` of the above |

A column that is not required is `... | None`. A column whose key clashes with a
pydantic attribute (`json`, `copy`, `schema`…) is reached as `typed.json_`; its
JSON key is unchanged. The model serialises through each field type, so
`typed.model_dump(mode="json", exclude_none=True)` is exactly the stored row.

*Proven by* `tests/contract/test_typed_values.py` and `tests/properties/test_values.py`; every row an agent's random calls stored, validated by its table's model: `battle/s05`.

## Change a schema

`apply` takes the schema you want. Without `prune` the document is a patch:
each table it names is replaced by its spec — so a column left out of a named
table is a drop — and the tables it does not name are left alone. With `prune`
it is the whole schema, and what it leaves out is dropped.

```python
# change_schema.py
import asyncio
import os

from forktex.database import close_engine, get_engine, init_engine, session

from forktex_grid import ApplyOptions, BadRequestError, ColumnSpec, Namespace, Schema, TableSpec, apply_migrations


def contacts(*columns: ColumnSpec) -> Schema:
    return Schema(tables=[TableSpec(slug="contact", columns=columns)])


NAME = ColumnSpec(key="name", type_id="text")
PHONE = ColumnSpec(key="phone", type_id="text")


async def main() -> None:
    await init_engine(os.environ["DATABASE_URL"])
    await apply_migrations(get_engine())

    async with session() as s:
        ns = Namespace(s, "address-book")
        await ns.apply(contacts(NAME, PHONE))

        # Add a column: re-apply the table with it.
        report = await ns.apply(contacts(NAME, PHONE, ColumnSpec(key="email", type_id="text")))
        print([c.target for c in report.plan.changes])  # ['column:contact.email']

        # Drop one: rehearse, then allow it.
        plan = await ns.apply(contacts(NAME), ApplyOptions(prune=True, dry_run=True))
        print([c.target for c in plan.plan.destructive])  # ['column:contact.email', 'column:contact.phone']
        try:
            await ns.apply(contacts(NAME), ApplyOptions(prune=True))
        except BadRequestError as refused:
            print(refused)  # refusing 2 destructive change(s) without allow_destructive: ...
        await ns.apply(contacts(NAME), ApplyOptions(prune=True, allow_destructive=True))

        # Rename: a diff cannot tell it from a drop and an add, so it is its own call.
        await ns.rename_column("contact", "name", "full_name")
        await s.commit()

        print([c.key for c in (await ns.describe()).table("contact").columns])  # ['full_name']

    await close_engine()


asyncio.run(main())
```

`describe()` returns the namespace's schema in the same form `apply` takes, so a
schema read from one namespace can be applied to another. A dropped column is
archived: its values stay in the stored rows, out of every read, until a column
with that key is declared again. A column's type or cardinality, and which table
a link points at, never change in place: add a new column and copy the values,
or [reshape the namespace](#reshape-a-namespace).

*Proven by* `tests/usage/test_linked_records.py` and `tests/properties/test_schema_convergence.py`; under live writers and readers: `battle/s06`.

## Link rows

A `ref` column holds the id of a row of another table, named in its config.
Every value written is checked: a live row of that table, in the same
namespace. `onDelete` says what archiving the target does to the rows that
point at it. A `many` ref column holds a list; a unique one makes the link
one-to-one.

```python
# links.py
import asyncio
import os

from forktex.database import close_engine, get_engine, init_engine, session, transaction

from forktex_grid import BadRequestError, ColumnSpec, Namespace, Query, Schema, TableSpec, apply_migrations, col, link

SCHEMA = Schema(
    tables=[
        TableSpec(slug="client", columns=[ColumnSpec(key="name", type_id="text")]),
        TableSpec(
            slug="invoice",
            columns=[
                ColumnSpec(key="number", type_id="text"),
                ColumnSpec(key="client", type_id="ref", config={"table": "client", "on_delete": "restrict"}),
            ],
        ),
    ]
)


async def main() -> None:
    await init_engine(os.environ["DATABASE_URL"])
    await apply_migrations(get_engine())
    async with session() as s:
        await Namespace(s, "billing").apply(SCHEMA)

    async with transaction() as s:
        ns = Namespace(s, "billing")
        clients, invoices = await ns.table("client"), await ns.table("invoice")

        acme = await clients.create({"name": "ACME SRL"})
        await invoices.create({"number": "INV-001", "client": acme.id})

        billed = await invoices.query(
            Query(where=link("client").where(col("name") == "ACME SRL"), select=("number", link("client").select("name")))
        )
        (invoice,) = billed.items
        print(invoice["number"], invoice.links["client"]["name"])  # INV-001 ACME SRL

        try:
            await clients.archive(acme.id)
        except BadRequestError as refused:
            print(refused)  # row is referred to by invoice.client (on_delete restrict)

    await close_engine()


asyncio.run(main())
```

`onDelete` is `restrict` (refuse while a live row points here), `cascade`
(archive the rows that point here, and theirs) or `set_null` (clear the value,
or take the id out of a list; refused for a required single-value column). Finding the rows
that point at one uses grid's JSONB index, so it does not scan. Writers racing
an archive never leave a live row pointing at an archived one.

*Proven by* `tests/contract/test_refs.py` and `tests/stress/test_link_contention.py`; cascades against a model of the data: `battle/s02`.

## Write a schema and its rows in one transaction

`batch` applies a schema patch and then row writes, in the caller's
transaction: all of it lands or none of it does. It is how a template is
installed with its seed rows.

```python
# batch.py
import asyncio
import os

from forktex.database import close_engine, get_engine, init_engine, transaction

from forktex_grid import Batch, ColumnSpec, CreateRow, Namespace, Schema, TableSpec, apply_migrations

STAGES = TableSpec(
    slug="stage",
    columns=[ColumnSpec(key="name", type_id="text", is_unique=True), ColumnSpec(key="position", type_id="integer")],
)


async def main() -> None:
    await init_engine(os.environ["DATABASE_URL"])
    await apply_migrations(get_engine())

    async with transaction() as s:
        report = await Namespace(s, "acme").batch(
            Batch(
                desired=Schema(tables=[STAGES]),
                rows=[
                    CreateRow(table="stage", values={"name": "lead", "position": 1}),
                    CreateRow(table="stage", values={"name": "won", "position": 2}),
                ],
            )
        )
        print([r.row["name"] for r in report.results if r.row])  # ['lead', 'won']

    await close_engine()


asyncio.run(main())
```

A batch builds its indexes inside the transaction, holding a write lock on
grid's shared row table until it commits. That is right for a new tenant's
tables; to add an index to a populated table, use `apply`. A batch arriving as
JSON — `{"desired": {...}, "rows": [{"op": "create", ...}]}` — becomes a `Batch`
with `Batch.model_validate(document)`.

*Proven by* `tests/contract/test_batch.py`; killed before its commit: `battle/s10`.

## Add your own field type

`type_id` is an open string checked against a registry; the built-ins are a
seed. Any type pydantic can validate becomes a field type in one line:

```python
from ipaddress import IPv4Address

from pydantic import AnyUrl, EmailStr  # EmailStr needs pydantic[email]

from forktex_grid import Capabilities, FilterOp, field_type, register_field_type

register_field_type(field_type("ipv4", IPv4Address))
register_field_type(field_type("url", AnyUrl))
register_field_type(
    field_type(
        "email",
        EmailStr,
        capabilities=Capabilities(
            filter_ops=frozenset({FilterOp.eq, FilterOp.ne, FilterOp.in_, FilterOp.is_null}),
            sortable=True,
            index_kinds=frozenset({"btree"}),
        ),
    )
)
```

A written value is validated by pydantic and stored as its JSON form; a refused
one is a `BadRequestError` naming the column; the table's model types it as
`IPv4Address`. Without `capabilities`, the type takes equality operators and a
btree index and does not sort.

When a type has settings, or rules pydantic does not express, write the handler:

```python
# ipv4_range.py
import ipaddress

from forktex.types import BaseWireValueObject

from forktex_grid import Capabilities, FieldTypeHandler, FilterOp, register_field_type


class Network(BaseWireValueObject):
    cidr: str = "0.0.0.0/0"


class AddressIn(FieldTypeHandler[Network]):
    type_id = "address_in"
    config_model = Network
    capabilities = Capabilities(filter_ops=frozenset({FilterOp.eq, FilterOp.is_null}), index_kinds=frozenset({"btree"}))

    def normalize(self, value: object, *, config: Network) -> str | None:
        if value is None:
            return None
        address = ipaddress.IPv4Address(str(value))  # ValueError -> BadRequestError naming the column
        if address not in ipaddress.IPv4Network(config.cidr):
            raise ValueError(f"{address} is outside {config.cidr}")
        return str(address)

    def python_type(self, config: Network) -> type:
        return ipaddress.IPv4Address


register_field_type(AddressIn())
# ColumnSpec(key="ip", type_id="address_in", config={"cidr": "10.0.0.0/8"})
```

`normalize` is the required contract: a written value in, its stored JSON form
out. `capabilities` says what the query engine may do with the type — an
operator outside `filter_ops` is refused, never mistranslated — and `pg_cast`
(`bigint`, `numeric`, `boolean`, `uuid`, or none: text) how Postgres compares it,
for filters, sorts and indexes alike. `python_type` is what the table's model
uses.

*Proven by* `tests/contract/test_types.py` and `tests/contract/test_typed_values.py`.

## Give an agent its own tables

`forktex_grid.agent` turns grid's methods into tools — one per public method of
`Namespace` and `Table`, with that method's parameters — plus a JSON schema per
tool for the model's tool list, and `run` to execute the call the model makes.
The model writes schema documents, rows and queries; it never writes Python or
SQL, and it works only inside the namespace you hand it.

```python
# agent.py
import asyncio
import os

from forktex.database import close_engine, get_engine, init_engine, session

from forktex_grid import Namespace, apply_migrations
from forktex_grid.agent import run, tool_schemas


async def main() -> None:
    await init_engine(os.environ["DATABASE_URL"])
    await apply_migrations(get_engine())

    tools = tool_schemas()  # {name: JSON schema}: give these to the model
    print(sorted(tools))

    # What a model might send while working on a research task:
    calls = [
        ("field_types", {}),
        (
            "apply",
            {
                "desired": {
                    "tables": [
                        {
                            "slug": "finding",
                            "columns": [
                                {"key": "claim", "typeId": "text", "isRequired": True},
                                {"key": "confidence", "typeId": "decimal"},
                                {"key": "source", "typeId": "text", "config": {"subtype": "url"}},
                            ],
                        }
                    ]
                }
            },
        ),
        ("create", {"table": "finding", "values": {"claim": "Grid stores rows as JSONB", "confidence": 0.9}}),
        (
            "query",
            {
                "table": "finding",
                "query": {
                    "where": {"column": "confidence", "op": "gte", "value": 0.5},
                    "orderBy": [{"column": "confidence", "direction": "desc"}],
                    "count": True,
                },
            },
        ),
    ]

    async with session() as s:
        ns = Namespace(s, "agent-run-42")  # one namespace per agent, task or conversation
        for name, args in calls:
            result = await run(ns, name, args)
            await s.commit()
        print(result["result"]["items"][0]["values"]["claim"])  # Grid stores rows as JSONB

    await close_engine()


asyncio.run(main())
```

Each call returns `{"result": …}` as JSON. A refused one — bad arguments
included — raises a grid error whose message is written to be read back to the
model, one `path: problem` per line: catch `forktex.error.AppError`, send the
message back as the tool's result, and the model corrects its next call.
`apply` commits, so the agent's session is a `session()` committed after each
call.

The schemas spell out each field type's column, `config` included, so a model
writes an `enum` with its `options` the first time. For an agent that should
only answer, give it `tool_schemas(READ_TOOLS)` and call
`run(ns, name, args, only=READ_TOOLS)`: a write it names anyway is refused.

[`examples/assistant.py`](../examples/assistant.py) is the whole loop with a
live model: a personal assistant whose memory is grid, one namespace per
*knowledge space*, that designs its own tables, answers from queries and
`count_by`, and reshapes a space with `migrate_to` once you confirm it at the
terminal (its instructions tell the model to show you a dry run first).

*Proven by* `tests/usage/test_agent_memory.py` and `tests/contract/test_agent_tools.py`; random and malformed calls, and a read-only agent: `battle/s05`.

## Reshape a namespace

`apply` changes a schema in place, but not a column's type or cardinality, nor
which table a link points at. When the shape itself is wrong — contacts that
should be people, two tables that are one — carry the namespace into a new one
with `migrate_to`. Rows are copied in one transaction; links follow their rows;
the source is only read.

```python
# reshape.py
import asyncio
import os

from forktex.database import close_engine, get_engine, init_engine, transaction

from forktex_grid import (
    ApplyOptions,
    ColumnSpec,
    IndexBuilds,
    Migration,
    Namespace,
    Schema,
    TableSource,
    TableSpec,
    apply_migrations,
)

OLD = Schema(
    tables=[
        TableSpec(slug="contact", columns=[ColumnSpec(key="full_name", type_id="text"), ColumnSpec(key="fax", type_id="text")]),
        TableSpec(slug="appointment", columns=[ColumnSpec(key="title", type_id="text"), ColumnSpec(key="with", type_id="ref", config={"table": "contact"})]),
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
        TableSource(target="agenda", source="appointment", columns={"title": "title", "person": "with"}, constants={"kind": "appointment"}),
        TableSource(target="agenda", source="task", columns={"title": "title"}, constants={"kind": "task"}),
    ],
)


async def main() -> None:
    await init_engine(os.environ["DATABASE_URL"])
    await apply_migrations(get_engine())

    async with transaction() as s:
        old = Namespace(s, "ana/v1")
        await old.apply(OLD, ApplyOptions(index_builds=IndexBuilds.inline))
        ana = await (await old.table("contact")).create({"full_name": "Ana", "fax": "021 000"})
        await (await old.table("appointment")).create({"title": "dentist", "with": str(ana.id)})
        await (await old.table("task")).create({"title": "renew passport"})

    async with transaction() as s:
        old, new = Namespace(s, "ana/v1"), Namespace(s, "ana/v2")
        rehearsal = await old.migrate_to(new, TO_NEW, ApplyOptions(dry_run=True))
        print({t.table: t.rows for t in rehearsal.tables})  # {'person': 1, 'agenda': 2}
        print(rehearsal.tables[0].columns_not_carried)  # {'contact': ('fax',)}
        await old.migrate_to(new, TO_NEW)
        print(await new.row_counts())  # {'agenda': 2, 'person': 1}
        kinds = await (await new.table("agenda")).count_by("kind")
        print({g.value: g.count for g in kinds.groups})  # {'appointment': 1, 'task': 1}

    await close_engine()


asyncio.run(main())
```

Read the dry run before the real one: `columns_not_carried` names every source
column no mapping reads (here the fax, on purpose), `tables_not_carried` every
table left behind, `links_dropped` every link whose row did not come along.
Copied rows keep their `external_ref` and timestamps. The migration builds the
new tables' indexes inside its transaction, holding writes to grid's shared row
table until it commits: see [operating](operating.md#reshaping-a-namespace)
before reshaping a large namespace.

*Proven by* `tests/usage/test_reshape.py` and `tests/contract/test_migrate.py`; a
random linked knowledge base through three shapes: `battle/s09`; killed halfway:
`battle/s10`.
