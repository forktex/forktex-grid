# Reference

Every name in `forktex_grid.__all__`, and the agent tools in `forktex_grid.agent`.
The ideas are in [concepts](concepts.md), the query language in
[query](query.md), worked files in the [guide](guide.md), production concerns in
[operating](operating.md).

- [Entry points](#entry-points): `apply_migrations`, `Namespace`, `Table`
- [Options](#options): `ApplyOptions`, `IndexBuilds`
- [The schema document](#the-schema-document): `Schema`, `TableSpec`, `ColumnSpec`, `IndexSpec`
- [The query document](#the-query-document): `Query`, `col`, `link`, the `where` nodes, `Embed`, `OffsetPaging`, `CursorPaging`, `SortKey`
- [Results](#results): `Row`, `Page`, `RowValues`, `ApplyReport`, `BatchReport`
- [The batch document](#the-batch-document): `Batch`, `RowOp`, `CreateRow`, `UpsertRow`, `PatchRow`, `ArchiveRow`
- [Field types](#field-types): the built-ins, `field_type`, `FieldTypeHandler`, `Capabilities`, `NoConfig`, `register_field_type`, `is_registered`
- [Vocabulary](#vocabulary): `FieldType`, `Cardinality`, `OnDelete`, `LinkMatch`, `SortDirection`, `FilterOp`
- [Errors](#errors)
- [Agent tools](#agent-tools): `forktex_grid.agent`

Every document model is a frozen `forktex.types.BaseWireValueObject`:
`model_dump(mode="json")` writes its camelCase JSON, `model_validate` reads it
back (camelCase or snake_case keys), and unknown keys are refused.

## Entry points

### `apply_migrations`

```text
async def apply_migrations(engine: AsyncEngine, schema: str = "forktex_grid") -> None
```

Creates grid's tables in the `forktex_grid` Postgres schema, or brings them up
to this version, using two connections from the engine's pool. Another `schema` puts them elsewhere; then give every engine
grid uses `execution_options={"schema_translate_map": {"forktex_grid": schema}}`,
because grid's queries name `forktex_grid`. Idempotent, and safe from every replica at once. Call it once
at startup. Grid's tables are not in your `BaseDBModel.metadata`, so
`create_all` does not create them.

### `Namespace`

```text
Namespace(session: AsyncSession, namespace: str)
```

One namespace's schema and tables, over your session. The namespace is any
non-empty string of up to 255 characters, typically a tenant id. Everything read or written through it, and
through the `Table`s it opens, is confined to it: another namespace's table or
row is `NotFoundError`. `namespace` (a property) is that string.

| Method | Does |
|---|---|
| `await describe() -> Schema` | Every live table, as the document `apply` takes |
| `await apply(desired: Schema, options: ApplyOptions = ApplyOptions()) -> ApplyReport` | Converges the namespace toward `desired` |
| `await batch(batch: Batch, options: ApplyOptions = ApplyOptions()) -> BatchReport` | A schema patch, then row writes, in the caller's transaction |
| `await table(slug) -> Table` | Opens a table; `NotFoundError` if there is none |
| `await rename_column(table, key, new_key) -> None` | Renames a column, carrying its values and indexes, in the caller's transaction; any value an earlier, dropped column left under `new_key` is discarded first |
| `await row_counts() -> dict[str, int]` | Live rows of every live table, by slug; one statement |
| `await migrate_to(target: Namespace, migration: Migration, options: ApplyOptions = ApplyOptions()) -> MigrationReport` | Copies this namespace's rows into `target`, an empty namespace on the same session, in a new schema: see [the migration document](#the-migration-document). Builds inline, like `batch`: asking it for `concurrent` is refused |

Each takes a document: a JSON one becomes the model with `Schema.model_validate(…)`,
`Batch.model_validate(…)` or `Query.model_validate(…)`.

### `Table`

One table of one namespace, from `Namespace.table`. It holds the table's spec as
it was when opened.

| Property | |
|---|---|
| `slug: str` | |
| `namespace: str` | |
| `spec: TableSpec` | Its columns and indexes: what a screen renders a form from |

| Method | Does |
|---|---|
| `await create(values, *, external_ref=None) -> Row` | Creates a row; `external_ref` is required when the table `extends` one |
| `await create_many(values, *, external_refs=None) -> list[Row]` | Creates several; `external_refs` pairs with `values` by position |
| `await get(row_id) -> Row` | The live row; `NotFoundError` for a row of another table or namespace |
| `await patch(row_id, *, values) -> Row` | Sets the given columns; the others keep their values |
| `await archive(row_id) -> None` | Archives it, applying the `onDelete` of every `ref` column pointing at it |
| `await get_by_external_ref(external_ref) -> Row \| None` | The row extending a host row |
| `await get_many_by_external_ref(external_refs) -> dict[str, Row]` | Those extending any of them, keyed by the host key as text; one query |
| `await upsert_by_external_ref(external_ref, *, values) -> Row` | Sets columns on that row, creating it if there is none |
| `await query(query: Query = Query()) -> Page` | A page of live rows: see [the query language](query.md) |
| `await count(where=None) -> int` | How many live rows `where` matches |
| `await count_by(column, where=None, limit=100) -> ValueCounts` | How many of them hold each value of `column`: see [the query language](query.md#count_by) |
| `model() -> type[RowValues]` | The table's pydantic model, built from its columns; cached per `Table` |
| `typed(row) -> RowValues` | `model().model_validate(row.values)`: the row as Python values |

**Values.** A write is checked against each column and refused with
`BadRequestError` naming it: an unknown column, a value its type cannot hold, a
missing or cleared required column, a `ref` to no live row of its table. A
column left out of a create gets its `default_value`. A value taken by a unique
column, or a host key already extended, is `AlreadyExistsError`. `values` may hold
Python values (`Decimal`, `date`, `UUID`, …); they are stored in their type's
JSON form. A `datetime` without an offset (`"2026-11-22T13:00:00"`, or a naive
`datetime`) is taken as UTC: send the offset when the time is local.

## Options

### `ApplyOptions`

How `apply` and `batch` change a schema. Every field defaults to the safe choice.

| Field | Default | |
|---|---|---|
| `prune: bool` | `False` | `False`: the document is a patch — each table it names is created or replaced by its spec, the others stay. `True`: it is the whole schema, and what it leaves out is dropped |
| `allow_destructive: bool` | `False` | Required to drop a table or column, make a column required or unique, change a column's config, or add a required column with no default. Without it they are refused with `BadRequestError` listing them, and nothing is applied. With it, a change the existing rows would break is still refused ([operating](operating.md#schema-changes-and-the-rows-already-there)) |
| `dry_run: bool` | `False` | `apply`: plan only, read `report.plan` — it lists destructive changes without refusing them and does not check existing rows, so the real apply can still refuse. `batch` and `migrate_to`: run everything, then roll it back — the report, or the error, is what the real run would give |
| `index_builds: IndexBuilds` | `concurrent` | See below. A `batch` always builds inline; asking it for `concurrent` is refused |

A column's type or cardinality, which table a `ref` column points at, and what
a table `extends` never change in place, even with `allow_destructive`: add a
new column (or table) and copy, or carry the namespace into a new shape with
`migrate_to`. A dropped table or column is archived: its values stay in the
stored rows, out of every read, until a column with the same key is declared
again (or renamed onto it), which starts that key empty.

### `IndexBuilds`

| Member | |
|---|---|
| `concurrent` | `CREATE INDEX CONCURRENTLY`, which does not block writes but cannot run in a transaction: `apply` **commits the session**, then builds on a connection of its own. Pass a `forktex.database.session()`; inside `transaction()` the commit is refused with `TransactionBoundaryError`. A session holding unflushed changes is refused |
| `inline` | Builds inside the caller's transaction, holding a write lock on grid's shared row table until commit: for new tables, tests and empty namespaces |

## The schema document

A spec that constructs is valid on its own — its field type is registered, its
config fits the type, its indexes cover columns the type can index — and none
names a namespace, so a document read from one namespace applies to another.
A column's `config` is stored in its type's canonical form, defaults left out.

```json
{
  "tables": [
    {
      "slug": "invoice",
      "label": "Invoice",
      "columns": [
        {"key": "number", "typeId": "text", "isRequired": true, "isUnique": true},
        {"key": "client", "typeId": "ref", "config": {"table": "client", "onDelete": "cascade"}},
        {"key": "tags", "typeId": "enum", "cardinality": "many", "config": {"options": ["urgent", "disputed"]}}
      ],
      "indexes": [{"columnKeys": ["client"]}]
    },
    {"slug": "client", "columns": [{"key": "name", "typeId": "text"}]}
  ]
}
```

### `Schema`

`tables: tuple[TableSpec, ...]`, slugs unique. `table(slug) -> TableSpec`;
`by_slug` is a dict of them. `apply` also checks that every `ref` column points
at a table of the schema it leads to — `check()` runs that check, and
`merged_with(patch, prune=False)` is the schema a patch apply converges to: each
table the patch names replaced by its spec, the others kept.

### `TableSpec`

| Field | Default | |
|---|---|---|
| `slug: str` | | Letters, digits, `_` and `-`, not starting with `-`, up to 63 characters; unique in the namespace |
| `label: str` | the slug | Display text; the model's docstring |
| `extends: str \| None` | `None` | A table you own (`schema.table`, or `table`) whose rows this table's rows extend, one each: every row then needs its `external_ref` |
| `columns: tuple[ColumnSpec, ...]` | `()` | Keys unique |
| `indexes: tuple[IndexSpec, ...]` | `()` | Over this table's single-valued columns |

`column(key) -> ColumnSpec`.

### `ColumnSpec`

| Field | Default | |
|---|---|---|
| `key: str` | | A plain identifier (letters, digits and `_`, not starting with a digit), up to 63 characters, unique in the table; the key of its value in each row. `id`, `external_ref`, `created_at`, `updated_at` are reserved |
| `label: str` | the key | Display text; the model field's title |
| `type_id: str` | | A registered field type |
| `cardinality: Cardinality` | `one` | `many`: the value is a list, each item checked |
| `is_required: bool` | `False` | |
| `is_unique: bool` | `False` | A unique index over the table's live rows; not for `many` columns or unindexable types |
| `config: dict` | `{}` | The type's settings |
| `default_value: Any` | `None` | Written when a create leaves the column out; checked against the type |
| `display_order: int` | `0` | Order of `spec.columns` |

### `IndexSpec`

| Field | Default | |
|---|---|---|
| `column_keys: tuple[str, ...]` | | One or more columns, in order |
| `kind: "btree" \| "trgm"` | `btree` | One of each column type's `index_kinds`; `trgm` needs `pg_trgm` |
| `is_unique: bool` | `False` | btree only |

Each becomes a Postgres index on the table's live rows, as does each unique
column. An index is identified by its kind and columns (`signature`).

## The query document

The language is described in [query](query.md); these are its types.

### `Query`

| Field | Default | |
|---|---|---|
| `where` | `None` | A `Comparison`, `And`, `Or`, `Not` or `Linked` |
| `order_by: tuple[SortKey, ...]` | `()` | |
| `select: tuple[str \| Embed, ...] \| None` | `None` (every column) | Column keys, and embeds |
| `page: OffsetPaging \| CursorPaging` | `OffsetPaging()` | |
| `count: bool` | `False` | Fill `Page.total` |

### Builders

| | |
|---|---|
| `col(key) -> Column` | A column or system field. Comparison operators `== != < <= > >=` and `.is_in`, `.not_in`, `.between`, `.is_null`, `.contains`, `.icontains`, `.startswith`, `.endswith`, `.fuzzy` return a `Comparison`; `.asc()`, `.desc()` a `SortKey` |
| `link(name) -> Link` | A `ref` column. `.where(c)` / `.some(c)`, `.every(c)`, `.none(c)` return a `Linked`; `.select(*keys)` an `Embed` |

Conditions combine with `&` (`And`), `|` (`Or`) and `~` (`Not`); chains stay flat.

### `where` nodes

| Model | JSON | Fields |
|---|---|---|
| `Comparison` | `{"column", "op", "value"}` | `column: str`, `op: FilterOp`, `value` |
| `And` | `{"and": [...]}` | `all_of`; `And.of(*conditions)` |
| `Or` | `{"or": [...]}` | `any_of`; `Or.of(*conditions)` |
| `Not` | `{"not": {...}}` | `negated`; `Not.of(condition)` |
| `Linked` | `{"link", "match", "where"}` | `link: str`, `match: LinkMatch = some`, `where` |

### `Embed`

`link: str`, `select: tuple[str, ...] | None` (`None`: every column of the
linked row). Returned under `Row.links[link]`.

### `OffsetPaging`, `CursorPaging`

`OffsetPaging(size=50, offset=0)`, `CursorPaging(size=50, after=None)`; on the
wire `{"by": "offset" | "cursor", ...}`, `by` optional (an `after` key means a
cursor). A query refuses a `size` outside 1–1 000 and an `offset` over
100 000 when it runs; the models themselves accept any integer.

### `SortKey`

`forktex.database.filters.SortKey`: `column: str`, `direction: SortDirection = asc`.

## Results

#### `Row`

| Field | |
|---|---|
| `id: UUID` | |
| `external_ref: str \| None` | The host row it extends, or the key it was created with |
| `values: dict[str, Any]` | By column key: live columns, or those `select` named |
| `created_at`, `updated_at: datetime` | UTC |
| `links: dict[str, Row \| tuple[Row, ...] \| None]` | Embedded rows, by link; empty unless `select` embeds |

Its JSON is camelCase (`externalRef`, `createdAt`). `row["key"]` and
`row.get("key")` read `values`. Values are JSON: a `decimal` is a string, a
`date` `"YYYY-MM-DD"`, a `ref` the target's id as a string; `Table.typed(row)`
gives Python values.

#### `Page`

`forktex.database.Page[Row]`, the page every forktex service returns: `items`,
`has_more` (exact), `next_cursor` (cursor paging), `total` (with `count`).

#### `RowValues`

The base of every table model `Table.model()` builds: a frozen
`BaseValueObject`, one field per column — named by its key, or `key_` where the
key is a pydantic attribute, aliased to the key — typed by its field type's
`python_type`, optional unless the column is required, titled by its label, and
serialised to JSON through the field type, so its JSON is the stored form.

#### `ApplyReport`

| Field | |
|---|---|
| `plan` | The ordered changes: `plan.changes`, each with `op`, `table`, `key`, `target` (`"column:invoice.total"`), `destructive`, and the `desired` and `actual` spec. In Python, `plan.destructive` is the destructive ones |
| `options: ApplyOptions` | As applied |
| `indexes` | One per index built concurrently: `table`, `index`, `columns`, `name`, `ok`, and `failure` — `duplicate_values` (existing rows break a unique index) or `build_failed`. The driver's message is logged, never returned |
| `complete: bool` | `False` when an index build failed. The schema is committed: fix the data and apply again |

#### `BatchReport`

`schema_report: ApplyReport | None`, `results`: one per row op, in order,
each with `op`, `table` and the `row` it wrote (`None` for an archive), and
`dry_run`. A dry run's rows were rolled back: their ids name nothing.

#### `ValueCounts`

`groups`: `ValueCount`s of `value` (the stored JSON form; `None` for rows
without one) and `count`, most frequent first, ties by value; `others`: what
fell past `limit`. The groups and `others` add up to every row matched — every
element, for a list column.

#### `MigrationReport`

| Field | |
|---|---|
| `source`, `target` | The two namespaces |
| `schema_report: ApplyReport` | The schema applied to `target` |
| `tables` | One `MigratedTable` per filled table: `table`, `sources`, `rows`, `columns_not_carried` (by source table: its columns no mapping read), `links_dropped` (link values whose row was not carried) |
| `tables_not_carried` | Source tables no mapping read |
| `dry_run` | |

## The batch document

#### `Batch`

| Field | Default | |
|---|---|---|
| `desired: Schema \| None` | `None` | A schema patch applied first, as `apply` would with the same options |
| `rows: tuple[RowOp, ...]` | `()` | Row writes, run in order after it |

```json
{
  "desired": {"tables": [{"slug": "stage", "columns": [{"key": "name", "typeId": "text"}]}]},
  "rows": [
    {"op": "create", "table": "stage", "values": {"name": "lead"}},
    {"op": "upsert", "table": "stage", "externalRef": "s-2", "values": {"name": "won"}}
  ]
}
```

#### `RowOp`

`CreateRow | UpsertRow | PatchRow | ArchiveRow`, told apart by `op`.

| Model | `op` | Fields |
|---|---|---|
| `CreateRow` | `create` | `table`, `values`, `external_ref` (required when the table `extends` one) |
| `UpsertRow` | `upsert` | `table`, `external_ref`, `values` |
| `PatchRow` | `patch` | `table`, `row_id`, `values` |
| `ArchiveRow` | `archive` | `table`, `row_id` |

## The migration document

`migrate_to` carries a namespace's live rows into another, empty namespace,
under a new schema, in one transaction — the way to change a column's type or
cardinality, split or merge tables, or rename everything at once. The source is
only read.

#### `Migration`

| Field | |
|---|---|
| `desired: Schema` | The new schema, applied to the target first |
| `tables: tuple[TableSource, ...]` | Where each new table takes its rows from; at least one |

#### `TableSource`

| Field | Default | |
|---|---|---|
| `target` | | A table of `desired` |
| `source` | | A table of the source namespace |
| `columns: dict[str, str] \| None` | `None` | New column key → source column key, or a system field (`id`, `external_ref`, `created_at`, `updated_at`). `None`: every key the two tables share |
| `constants: dict` | `{}` | New column key → a value every copied row gets; for a link column, the id of a source row, re-pointed like any link |

Several `TableSource`s may share a `target`: their rows are merged. Each copied
row keeps its `external_ref`, `created_at` and `updated_at`, and gets a new id;
every `ref` value in the new schema is re-pointed at the copy of the row it
named, whichever source it came from. A link whose row was not carried is
dropped and counted. Links inside a table, or around a cycle of tables, are set
once every row exists, so such a link cannot be required in `desired`. Each
value is validated by its new column: one that does not fit is refused with the
source row's id. A single value copied into a list column becomes a list of
one; a list copied into a single-value column gives its one item, nothing for
an empty list, and is refused when it holds more; a column whose cardinality
does not change is copied as it is (a `json` value that is a list stays one).
Tables are filled in dependency order, each cycle of links as a group, so only
a link inside a cycle waits. Two migrations into one
namespace take turns: the second finds it no longer empty.

```json
{
  "desired": {"tables": [
    {"slug": "person", "columns": [{"key": "name", "typeId": "text", "isRequired": true}]},
    {"slug": "agenda", "columns": [
      {"key": "kind", "typeId": "enum", "config": {"options": ["appointment", "task"]}},
      {"key": "title", "typeId": "text"},
      {"key": "person", "typeId": "ref", "config": {"table": "person"}}]}]},
  "tables": [
    {"target": "person", "source": "contact", "columns": {"name": "full_name"}},
    {"target": "agenda", "source": "appointment", "columns": {"title": "title", "person": "with"}, "constants": {"kind": "appointment"}},
    {"target": "agenda", "source": "task", "columns": {"title": "title"}, "constants": {"kind": "task"}}
  ]
}
```

The target's indexes are built inside the transaction, as a `batch`'s are; see
[operating](operating.md#reshaping-a-namespace) for what that holds.

## Field types

| `type_id` | Filter operators | Sort | Index kinds | Python | `config` |
|---|---|---|---|---|---|
| `text` | `eq ne in not_in contains icontains startswith endswith is_null fuzzy` | yes | `btree`, `trgm` | `str` | `maxLength`; `subtype`: `email` (checked), or a hint such as `url`, `phone`, `multiline` |
| `integer` | `eq ne in not_in lt lte gt gte between is_null` | yes | `btree` | `int` | |
| `decimal` | as `integer` | yes | `btree` | `Decimal` | `precision` (18), `scale` (6) |
| `boolean` | `eq ne is_null` | yes | | `bool` | |
| `date` | `eq ne lt lte gt gte between is_null` | yes | `btree` | `date` | |
| `datetime` | as `date` | yes | `btree` | `datetime` | Stored in UTC |
| `uuid` | `eq ne in not_in is_null` | no | `btree` | `UUID` | |
| `enum` | `eq ne in not_in is_null` | yes | `btree` | `Literal[...]` | `options`: the allowed strings (required) |
| `json` | none | no | | any JSON | |
| `ref` | `eq ne in not_in is_null` | no | `btree` | `UUID` | `table` (required), `onDelete` (`restrict`) |

#### `field_type`

```text
field_type(type_id: str, annotation, *, capabilities: Capabilities | None = None, pg_cast: PgCast | None = None) -> FieldTypeHandler
```

A field type from any type pydantic validates: a written value is validated by
`TypeAdapter(annotation)` and stored as its JSON form; the table model types it
as `annotation`. Default capabilities: equality operators and a btree index, not
sortable. Register it with `register_field_type`.

#### `register_field_type`

```text
register_field_type(handler: FieldTypeHandler, *, replace: bool = False) -> None
```

Adds a type under `handler.type_id`, at import time. A second registration of a
`type_id` is refused unless `replace=True`.

#### `is_registered`

```text
is_registered(type_id: str) -> bool
```

#### `FieldTypeHandler`

`FieldTypeHandler[C]`, the base of a field type, generic over its config model.
One instance per type, held by the registry.

| Member | |
|---|---|
| `type_id: ClassVar[str]` | The stored identifier; never renamed once columns use it |
| `capabilities: ClassVar[Capabilities]` | What the query engine may do with it |
| `config_model: type[C]` | Validates a column's `config`: a pydantic model (`BaseValueObject` or a subclass); `NoConfig` for none. `parse_config(raw)` validates through it |
| `pg_cast: ClassVar[PgCast \| None]` | How Postgres compares the stored text, for filters, sorts and indexes alike: `bigint`, `numeric`, `boolean`, `uuid`, or `None` (as text) |
| `normalize(value, *, config) -> JSON` | A written value → its stored form; `ValueError` for one it cannot hold |
| `python_type(config) -> TypeForm` | The Python type the table model uses; `Any` unless overridden |

A `ValueError` from `normalize` becomes `BadRequestError` naming the column, so a
type never imports grid's errors.

#### `Capabilities`

| Field | Default | |
|---|---|---|
| `filter_ops: frozenset[FilterOp]` | empty | Any other operator is refused; empty: not filterable |
| `sortable: bool` | `False` | Also what cursor paging needs |
| `index_kinds: frozenset["btree" \| "trgm"]` | empty | Empty: no index, and not unique |

#### `NoConfig`

The config of a type that takes none: an empty object.

## Vocabulary

String enums: a document may use the value as well as the member.

| Enum | Members |
|---|---|
| `FieldType` | The built-in `type_id`s. Only ever added to: each is stored in your database |
| `Cardinality` | `one`, `many` |
| `OnDelete` | `restrict` (refuse while a live row points here), `cascade` (archive those rows too), `set_null` (clear the value, or take the id out of a list; refused for a required single-value column — a required list may be left empty) |
| `LinkMatch` | `some`, `every`, `none` |
| `IndexBuilds` | `concurrent`, `inline` |
| `SortDirection` | `asc`, `desc` |
| `FilterOp` | `forktex.database.filters.FilterOp`, shared by every forktex consumer: `eq`, `ne`, `lt`, `lte`, `gt`, `gte`, `between`, `in`, `not_in`, `contains`, `icontains`, `startswith`, `endswith`, `is_null`, `fuzzy` |

## Errors

All are `forktex.error`'s, so `forktex.api` renders them with their status, and
one `AppError` handler covers them beside your own.

| Error | Status | When |
|---|---|---|
| `NotFoundError` | 404 | A table or row that does not exist **in this namespace**, or the column `rename_column` names. A column a write or query names that the table lacks is `BadRequestError` |
| `BadRequestError` | 400 | An invalid spec or document, a value a column refuses, an operator a column does not take, a destructive change without `allow_destructive`, a `ref` to no live row, an archive `onDelete` refuses, a query naming what does not exist |
| `AlreadyExistsError` | 409 | A value a unique column or index already holds, a host key already extended, a slug already taken, a field type registered twice |
| `ConflictError` | 409 | Postgres broke a deadlock or serialization conflict by failing this statement. Grid rolls back only its own savepoint, so locks your transaction took earlier are still held: roll the transaction back, then retry |
| `ServiceUnavailableError` | 503 | A concurrent `apply`'s schema lock came back from another database server connection: grid's connections go through a pooler in transaction mode ([operating](operating.md#requirements)). The change is committed; fix the connection setup |

## Agent tools

### `forktex_grid.agent`

Outside `__all__`: `from forktex_grid.agent import READ_TOOLS, TOOLS, run, tool_schemas`.

| | |
|---|---|
| `tool_schemas(only=None) -> dict[str, dict]` | Each tool's input as JSON schema, for a model's tool list; `only` keeps those tools |
| `await run(namespace, op, args, *, only=None) -> dict` | Validates `args`, runs the tool in `namespace`, returns `{"result": …}` as JSON; a tool outside `only` is refused |
| `TOOLS: dict[str, type[BaseWireValueObject]]` | Each tool's input model |
| `READ_TOOLS: frozenset[str]` | The tools that cannot write: for an agent that only answers |

Every refusal is a grid error, `BadRequestError` for arguments the tool's schema
does not accept, with one `path: problem` line per problem
(`invalid arguments for 'query': query.limt: Extra inputs are not permitted`)
and the same list in `details["problems"]`.

The schemas name each field type's column as its own variant, chosen by
`typeId`, with that type's `config` (an `enum` column requires
`config.options`, a `ref` column `config.table`); a type registered later
appears the next time `tool_schemas()` is called. Generated titles are left
out. All seventeen tools take about 32 000 characters of schema; `only` trims
what a given agent needs.

Each tool is a public method, derived from its signature: the tool's name is the
method's name, its input the method's parameters (camelCase, snake_case
accepted; a `Table` method's tool also takes `table`, the slug), its result the
method's return value as JSON.

| Tool | Calls |
|---|---|
| `describe`, `apply`, `batch`, `rename_column`, `row_counts` | `Namespace`'s methods of those names |
| `create`, `create_many`, `get`, `patch`, `archive`, `query`, `count`, `count_by`, `get_by_external_ref`, `get_many_by_external_ref`, `upsert_by_external_ref` | `Table`'s, on the table named by `table` |
| `field_types` | Every type: its capabilities and its `config` JSON schema |

A model works only in the namespace it is given. `migrate_to` is not a tool: it
names a second namespace, which only your code may choose — wrap it with your
own policy (a human's confirmation, say), as
[`examples/assistant.py`](../examples/assistant.py) does. Within its namespace
the `apply` tool can do anything `apply` can — with `prune` and
`allowDestructive`, archive every table — so give a model that should not
reshape its memory `only=` a set without `apply` and `batch`, or check those
two options before running the call. `apply` defaults to concurrent
index builds and commits: give the agent a `session()` and commit after each
call. A tool's name and parameters change only when the method's do, which
semantic versioning already covers.
