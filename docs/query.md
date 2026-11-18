# The query language

One `Query` asks one table for rows. It is a document with five parts, written
as JSON (what an agent or an HTTP client sends) or with a typed Python builder
(what your code writes). Both produce the same `Query`; tests hold them equal.

```python
from forktex_grid import Query, col, link

page = await invoices.query(
    Query(
        where=(col("total") >= 100) & link("client").where(col("country") == "RO"),
        order_by=(col("issued").desc(),),
        select=("number", "total", link("client").select("name")),
        count=True,
    )
)
```

```json
{
  "where": {"and": [
    {"column": "total", "op": "gte", "value": 100},
    {"link": "client", "where": {"column": "country", "op": "eq", "value": "RO"}}
  ]},
  "orderBy": [{"column": "issued", "direction": "desc"}],
  "select": ["number", "total", {"link": "client", "select": ["name"]}],
  "count": true
}
```

`Query.model_validate(document)` reads the JSON; `query.model_dump(mode="json")`
writes it. Unknown keys are refused, so a typo is an error, not a silently
ignored filter.

| Part | Default | |
|---|---|---|
| [`where`](#where) | every live row | A tree of conditions |
| [`orderBy`](#orderby) | by id | Sort keys |
| [`select`](#select) | every column, nothing embedded | Columns to return, linked rows to embed |
| [`page`](#page) | offset 0, size 50 | Offset or cursor paging |
| [`count`](#count) | `false` | Also return how many rows match |

## `where`

A tree of nodes:

| Node | JSON | Python |
|---|---|---|
| Comparison | `{"column": "total", "op": "gte", "value": 100}` | `col("total") >= 100` |
| And | `{"and": [n, n, …]}` | `a & b & c` |
| Or | `{"or": [n, n, …]}` | `a \| b` |
| Not | `{"not": n}` | `~a` |
| Link | `{"link": "client", "match": "some", "where": n}` | `link("client").where(n)` |

`a & b & c` builds one flat `and`, as you would write it by hand.

### Operators

Which operators a column takes is its field type's decision; anything else is
refused with the column and the operator named.

| Operator | Python | Value | Taken by |
|---|---|---|---|
| `eq`, `ne` | `==`, `!=` | one value | every type but `json` |
| `lt`, `lte`, `gt`, `gte` | `<`, `<=`, `>`, `>=` | one value | `integer`, `decimal`, `date`, `datetime` |
| `between` | `.between(lo, hi)` | `[lo, hi]`, inclusive | as above |
| `in`, `not_in` | `.is_in([…])`, `.not_in([…])` | a list (up to 1 000) | `text`, `integer`, `decimal`, `uuid`, `enum`, `ref` |
| `contains`, `icontains` | `.contains(s)`, `.icontains(s)` | text | `text` |
| `startswith`, `endswith` | `.startswith(s)`, `.endswith(s)` | text | `text` |
| `fuzzy` | `.fuzzy(s)` | text, trigram similarity; refused unless the database has `pg_trgm` | `text` |
| `is_null` | `.is_null(True \| False)` | a boolean | every type but `json` |

A value is checked by the column's type before it reaches SQL: `col("total") >=
"lots"` is refused, `col("day") > "2026-01-15"` compares as dates, not strings.
A `datetime` without an offset is taken as UTC, as it is when written: send
`"2026-11-22T13:00:00+02:00"` for a local time.

**Nulls.** A comparison with a missing value is neither true nor false, as in
SQL: `col("count") != 3` does not match a row with no `count`; ask for it with
`col("count").is_null()`. `in` over an empty list matches nothing, and
`~col(…).is_in([])` everything.

A `many` column (a list) is not filtered or sorted, though `count_by` counts
its elements; a `json` column is stored and returned only.

### System fields

`id`, `external_ref`, `created_at` and `updated_at` filter and sort like
columns: `id` as a `uuid`, `external_ref` as `text`, the timestamps as
`datetime`.

```python
col("created_at") >= datetime(2026, 11, 1, tzinfo=UTC)
col("external_ref").startswith("CLIENT-")
```

### Links

A `link` node filters by the row a `ref` column points at. Its `where` is an
ordinary filter **on the linked table**, so its columns are that table's:

```python
link("client").where(col("country") == "RO")      # invoices of Romanian clients
```

For a `many` ref column, `match` says how many linked rows must satisfy it —
the vocabulary Prisma uses:

| `match` | Python | A row matches when |
|---|---|---|
| `some` (default) | `link("tags").some(c)` | at least one linked row satisfies `c` |
| `every` | `link("tags").every(c)` | no linked row fails `c` — also true with none linked |
| `none` | `link("tags").none(c)` | no linked row satisfies `c` |

On a single ref column the same three apply to its one target: `none` also
matches a row with no target. A link compiles to an `EXISTS` subquery, so
Postgres does the work and archived targets never match.

**One level.** A link's `where` may not contain another link. Following links
further is where a JSON filter would start re-implementing a query planner;
when you need it, query the linked table and filter by its ids.

## `orderBy`

A list of `{"column": …, "direction": "asc" | "desc"}`, or `col(…).asc()` /
`col(…).desc()`. Columns must be sortable (every scalar type but `uuid`, `ref`
and `json`); system fields sort too. Nulls come last ascending and first
descending, as Postgres orders them. Ties are broken by row id, in the
direction of the last key — the same order whichever way you page.

## `select`

What each returned row carries. Absent: every column.

- A **column key** returns that column in `row.values`. System fields are always
  on the row (`row.id`, `row.external_ref`, `row.created_at`, `row.updated_at`);
  naming one in `select` is accepted and changes nothing.
- An **embed** — `{"link": "client", "select": ["name"]}` or
  `link("client").select("name")` — returns the row a `ref` column points at,
  under `row.links["client"]`, with its own `select` (absent: every column of
  the linked row). A `many` ref embeds a list; a row with nothing linked embeds
  `None` (or an empty list). `row.values["client"]` stays the id, and is there
  only if `"client"` is also selected.

```python
(invoice,) = (await invoices.query(Query(
    where=col("number") == "INV-1",
    select=("number", link("client").select("name", "country")),
))).items
invoice.values                    # {"number": "INV-1"}
invoice.links["client"].values    # {"name": "Acme", "country": "RO"}
```

An embed is the `Query` shape applied to a link: it takes `select` today, and
the same keys as a `Query` are where `where`, `orderBy` and `page` on an embed
will go.

## `page`

| Paging | JSON | Python | For |
|---|---|---|---|
| Offset | `{"by": "offset", "size": 50, "offset": 100}` | `OffsetPaging(size=50, offset=100)` | Numbered pages, small offsets |
| Cursor | `{"by": "cursor", "size": 50, "after": "…"}` | `CursorPaging(size=50, after=…)` | Walking a whole table, infinite scroll |

`by` may be left out: a page with an `after` key — `null` for the first page — is
a cursor page, any other an offset page. `size` is 1 to 1 000; `offset` at most 100 000.

The result is a `Page`: `items`, `hasMore` (exact: grid reads one row past the
page), `nextCursor` (cursor paging), `total` (with `count`). To walk with a
cursor, pass each page's `nextCursor` back as `after` until `hasMore` is false;
every row is visited once, in the order offset paging would give. Cursor
paging needs every sort key in one direction.

## `count`

`"count": true` adds `total`: how many rows match `where`, over all pages.
`table.count(where)` returns just that number.

## `count_by`

How many of the matching rows hold each value of one column — the numbers
behind a facet, a chart or "how many tasks per status":

```python
await tasks.count_by("status", col("due") < date(2026, 12, 1))
```

```json
{"groups": [{"value": "todo", "count": 12}, {"value": "done", "count": 7}, {"value": null, "count": 2}],
 "others": 0}
```

One statement, grouped in Postgres: never count a page of rows yourself, which
is wrong as soon as there is a second page. Groups come most frequent first,
ties by value with no value last; `limit` (1–1 000, default 100) caps
the groups and `others` counts the rest, so the two always add up to every row
matched. A list column counts each element: a row tagged `["a", "b"]` counts
once for each. Any column with equality groups (not `json`); `where` may
follow links. The agent tool is `count_by`, taking `table`, `column`, `where`
and `limit`.

## Limits and refusals

Every refusal is a `BadRequestError` whose message names what was wrong — the
column, the operator, the link — written to be read back to a model:

- an unknown column, link or embed; an operator its type does not take;
- a value its type cannot hold;
- `fuzzy` on a database without the `pg_trgm` extension;
- a link inside a link; a link or embed over a column that is not a `ref`;
- sorting by a column that does not sort; cursor paging with mixed directions;
- a page size outside 1–1 000, an offset over 100 000, a cursor that does not
  decode.

## For agents

`forktex_grid.agent`'s `query` tool takes `{"table": …, "query": {…}}` — the
document above — `count` takes `{"table": …, "where": {…}}`, and `count_by`
adds `column` and `limit`. Their JSON
Schema, from `tool_schemas()`, describes every node, so a model is told the
whole language in its tool list.
