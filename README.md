<p align="center">
  <img src="https://raw.githubusercontent.com/forktex/forktex-grid/master/docs/assets/grid-banner.png" alt="forktex-grid" height="120">
</p>

<p align="center">
  <a href="https://pypi.org/project/forktex-grid/"><img src="https://img.shields.io/pypi/v/forktex-grid.svg" alt="PyPI"></a>
  <a href="https://pypi.org/project/forktex-grid/"><img src="https://img.shields.io/pypi/pyversions/forktex-grid.svg" alt="Python"></a>
  <a href="https://github.com/forktex/forktex-grid/blob/master/LICENSE"><img src="https://img.shields.io/pypi/l/forktex-grid.svg" alt="License"></a>
</p>

<p align="center"><em>Tables, columns and rows declared at runtime, stored in PostgreSQL.</em></p>

Your tenants, your users or your agents declare tables as data — typed
columns, links between tables, indexes — and get validated writes, real unique
indexes and one query language, with no migration and no deploy. Everything
lives in one schema of your own Postgres database; your tables are never
touched.

```bash
pip install forktex-grid
```

```python
from forktex_grid import ColumnSpec, Namespace, Query, Schema, TableSpec, col

acme = Namespace(session, "acme")
await acme.apply(Schema(tables=[TableSpec(slug="invoice", columns=[
    ColumnSpec(key="number", type_id="text", is_unique=True),
    ColumnSpec(key="total", type_id="decimal"),
])]))

invoices = await acme.table("invoice")
await invoices.create({"number": "INV-001", "total": "250.00"})
page = await invoices.query(Query(where=col("total") >= 100))
```

## Requirements

| | |
|---|---|
| **Python** | 3.14 |
| **PostgreSQL** | 14 or newer. Local, in a container or managed. |
| **Connection** | Direct, or through a pooler in session mode, with a pool of at least two connections. A transaction-mode pooler cannot hold the lock a schema change takes. |
| **Permission** | Creating the `forktex_grid` schema on first start, or owning one created for it. Nothing else in the database is touched. |
| **Driver** | `asyncpg`, through [`forktex.database`](https://github.com/forktex/forktex-py); URLs use `postgresql+asyncpg://`. |
| **Extension** | `pg_trgm`, only for the `fuzzy` filter and `trgm` indexes. Grid does not install it. |

## What you can build

| You want to | Read |
|---|---|
| Run a first table in one file | [Your first table](https://github.com/forktex/forktex-grid/blob/master/docs/guide.md#your-first-table) |
| Serve tables from a FastAPI app, one namespace per tenant | [Run inside a web app](https://github.com/forktex/forktex-grid/blob/master/docs/guide.md#run-inside-a-web-app) |
| Let each tenant add its own fields to your clients, orders, … | [Add tenant fields](https://github.com/forktex/forktex-grid/blob/master/docs/guide.md#add-tenant-fields-to-a-table-you-already-own) |
| Filter, follow links, embed linked rows, page and count | [Ask questions](https://github.com/forktex/forktex-grid/blob/master/docs/guide.md#ask-questions-filters-links-embeds-pages) and [the query language](https://github.com/forktex/forktex-grid/blob/master/docs/query.md) |
| Validate a form with a table's own pydantic model | [Read values as Python](https://github.com/forktex/forktex-grid/blob/master/docs/guide.md#read-values-as-python-and-validate-forms) |
| Add, drop or rename columns | [Change a schema](https://github.com/forktex/forktex-grid/blob/master/docs/guide.md#change-a-schema) |
| Link rows, and decide what archiving one does to the others | [Link rows](https://github.com/forktex/forktex-grid/blob/master/docs/guide.md#link-rows) |
| Install a template with its seed rows, all or nothing | [Write a schema and its rows in one transaction](https://github.com/forktex/forktex-grid/blob/master/docs/guide.md#write-a-schema-and-its-rows-in-one-transaction) |
| Store a value grid has no type for | [Add your own field type](https://github.com/forktex/forktex-grid/blob/master/docs/guide.md#add-your-own-field-type) |
| Let an LLM agent design, fill and query its own tables | [Give an agent its own tables](https://github.com/forktex/forktex-grid/blob/master/docs/guide.md#give-an-agent-its-own-tables), and [a whole assistant](https://github.com/forktex/forktex-grid/blob/master/examples/assistant.py) |
| Change a shape `apply` cannot change in place | [Reshape a namespace](https://github.com/forktex/forktex-grid/blob/master/docs/guide.md#reshape-a-namespace) |

## How it works

| Concept | What it is |
|---|---|
| **Namespace** | One tenant's, user's or agent's tables. Another namespace's table or row does not exist for it. |
| **Schema** | The tables you want, as a document. `apply` makes the namespace match it; applying it again changes nothing. |
| **Table** | Typed columns and indexes. A unique column is a real Postgres unique index. |
| **Row** | A JSON object keyed by column, checked on every write. Read it as JSON, or as Python values through the table's model. |
| **Link** | A `ref` column holding another row's id, checked on write, with what archiving the target does: refuse, cascade or clear. |
| **Query** | Filters, links, embeds, sorting, pages and counts — one document, written as JSON by an agent or with `col()` in code. |

The ideas in ten minutes: [concepts](https://github.com/forktex/forktex-grid/blob/master/docs/concepts.md).

## Things to know

| | |
|---|---|
| **Use it for the shape you don't know at deploy time** | When you know your tables, write them: a migration gives you foreign keys, column types and a planner that knows your data. Grid's rows are JSONB in one shared table. |
| **Choose the namespace in one place** | Derive it from your authentication, never from a request parameter: grid isolates namespaces, but cannot know which one is the caller's. |
| **Dropping is archiving** | A dropped table, column or row is archived and skipped by every read. Destructive schema changes need `allow_destructive`. |
| **Some changes are not made in place** | A column's type, a link's target table and what a table extends are changed by copying into a new namespace with `migrate_to`. |
| **`apply` commits by default** | It builds indexes `CONCURRENTLY`, so give it a session it may commit; inline builds (`index_builds="inline"`) and row writes join your transaction. |

## Documentation

| Page | For |
|---|---|
| [Concepts](https://github.com/forktex/forktex-grid/blob/master/docs/concepts.md) | How grid thinks, in ten minutes |
| [Guide](https://github.com/forktex/forktex-grid/blob/master/docs/guide.md) | Runnable examples for each way to use it |
| [Query language](https://github.com/forktex/forktex-grid/blob/master/docs/query.md) | Every filter, link match, embed, page and count |
| [Reference](https://github.com/forktex/forktex-grid/blob/master/docs/reference.md) | Every public name, option and error |
| [Operating](https://github.com/forktex/forktex-grid/blob/master/docs/operating.md) | Schema changes on a live database, performance, limits, failures |

## Stability

From 1.0.0, semantic versioning covers every name in `forktex_grid.__all__`,
the schema and query documents, the agent tool names and the `forktex_grid`
database schema. Anything else is internal. Changes are listed in the
[CHANGELOG](https://github.com/forktex/forktex-grid/blob/master/CHANGELOG.md).

## Contributing

Working on `forktex-grid` itself? Start with
[`docs/development.md`](https://github.com/forktex/forktex-grid/blob/master/docs/development.md).

## Licence

AGPL-3.0-or-later, or a commercial licence from FORKTEX S.R.L.
(info@forktex.com). See [LICENSE](https://github.com/forktex/forktex-grid/blob/master/LICENSE) and [NOTICE](https://github.com/forktex/forktex-grid/blob/master/NOTICE).
