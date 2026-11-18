# Concepts

Grid is small on purpose: a handful of ideas, each with one name. Read this page
once and the [guide](guide.md), the [query language](query.md) and the
[reference](reference.md) read as details of what you already know.

## The picture

```text
 your code / your agent
        │
        │   documents in, JSON or typed Python
        │   ┌──────────────┬──────────────┬──────────────────┐
        ▼   │  Schema      │  row writes  │  Query           │
   Namespace("acme")  ──── apply / batch ── create, upsert … ── query, count, count_by
        │                  └── migrate_to ──▶ Namespace("acme/v2")
        │
        ▼
   forktex_grid schema in your Postgres
     grid_table   tables of every namespace (slug, label, extends, indexes)
     grid_column  their columns (key, type, cardinality, config, flags)
     grid_row     every row of every table: a JSONB payload by column key
     + one real Postgres index per unique column and declared index
```

## Namespace — the isolation boundary

Everything goes through a `Namespace(session, "acme")`. The string is yours:
a tenant id, a user id, an agent run. A table, row or column of another
namespace is `NotFoundError` — not forbidden, *absent* — and a link can only
point inside its own namespace. Grid cannot know whether the namespace you
pass is the right one: derive it in one place, from authentication.

## Four documents

Grid's whole surface is four kinds of document. Each is a pydantic model you
build in Python and the camelCase JSON an agent or HTTP client sends; one
validates into the other.

| Document | Says | Taken by |
|---|---|---|
| `Schema` | Which tables exist, with which columns and indexes | `Namespace.apply` |
| `Batch` | A schema patch, then row writes — create, upsert by host key, patch, archive — all or nothing | `Namespace.batch` |
| `Query` | Which rows, in which order, which columns, which page | `Table.query`, `count`, `count_by` |
| `Migration` | A new schema, and which old table fills each new one, column by column | `Namespace.migrate_to` |

Single row writes are also plain `Table` methods (`create`, `patch`, …): a
`Batch` is for when several must land together.

### Schema: declare, then converge

A `Schema` is the shape you want. `apply` compares it with what the namespace
holds and makes the difference: it is idempotent (apply twice, the second plans
nothing), it is a patch unless you say `prune`, and it refuses anything that
loses data — dropping a table or column, tightening a column — unless you say
`allowDestructive`. A `dryRun` returns the plan and changes nothing. All of
that is one `ApplyOptions`.

### Tables, columns and field types

A table has columns; a column has a key, a **field type** and a cardinality
(`one` value or a `many` list). The field type decides everything about a
value: what it accepts, how it is stored, which filter operators it takes,
whether it sorts, and which Postgres index it can have. Grid ships ten —
`text`, `integer`, `decimal`, `boolean`, `date`, `datetime`, `uuid`, `enum`,
`json`, `ref` — and any pydantic type becomes an eleventh in one line.

Every row also carries four **system fields** — `id`, `external_ref`,
`created_at`, `updated_at` — which a query names like columns. Those keys are
reserved: no column may take them.

### Links

A `ref` column holds the id of a row of another table in the namespace. Every
write checks it points at a live row; `onDelete` says what archiving the target
does — `restrict` it, `cascade` the archive, or `set_null` the link. Locks make
that true under concurrency: a write takes its targets `FOR SHARE`, an archive
takes its row `FOR UPDATE`, so no live row ever points at an archived one.

### Extending a table you own

A table that `extends` one of yours — `extends="public.client"` — adds fields
to your rows without touching them: each grid row is keyed by your row's
primary key (`external_ref`), and `upsert_by_external_ref` is the "save"
button of a custom-fields screen. Your table is never read or altered.

### Query: one question, two spellings

A `Query` says `where` (a tree of comparisons joined by `and`/`or`/`not`, and
`link` nodes that filter through a `ref` column), `orderBy`, `select` (columns,
and linked rows to embed), `page` (offset or cursor) and whether to `count`.
The Python builder — `col("total") >= 100`, `link("client").where(...)` — writes
exactly the JSON an agent would. [The query language](query.md) has it all.

## Values in layers

```text
  stored / wire      JSON           row.values  {"total": "250.00", "issued": "2026-11-01"}
        ▲ ▼                         every write is normalised into this form
  python             typed model    table.typed(row).total  -> Decimal("250.00")
                                    table.model()           -> a pydantic model, JSON Schema included
```

The stored form is JSON, chosen by the field type; it is what the wire carries
and what Postgres indexes. On top, `table.model()` builds a pydantic model from
the table's columns, so a row reads back as `Decimal`, `date`, `UUID`, a
`Literal` of an enum's options — and a form validates against the same model
before it writes. The model serialises back through each field type, so the
two layers cannot disagree.

## What happens on a write

1. The table's columns are loaded with their field types.
2. Each value is normalised by its type; an unknown column, a value the type
   refuses or a missing required one is a `BadRequestError` naming the column.
3. Each `ref` value is checked against a live target, locked `FOR SHARE`.
4. The row is written; a unique column or index that already holds the value
   is an `AlreadyExistsError`.

All inside a savepoint: a refused write leaves your transaction usable.

## What happens on a query

1. The `Query` is validated: every column, link and embed must exist, every
   operator must be one its field type takes.
2. It compiles to **one** SQL statement over `grid_row`: comparisons cast the
   stored JSON the way the type compares (the same expression grid's indexes
   are built on, so Postgres can use them), links become `EXISTS` subqueries.
3. The page is fetched one row past its size, to know `hasMore` exactly;
   `count` adds one `count(*)` over the same filter; each embed is one more
   query for all the page's linked rows.

## Where grid stops

Grid does not join your tables into its queries, aggregate beyond counting
(in total, or per value with `count_by`), or
follow links more than one level. Those are deliberate: each is a place where a
JSON layer would start pretending to be a query planner. When you know your
tables, write them with `forktex.database` and a migration.
