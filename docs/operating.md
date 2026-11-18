# Operating

What grid puts in your database, what it needs from it, and how its schema
changes behave on a populated deployment.

## What is in the database

Everything lives in one Postgres schema, `forktex_grid`, beside yours:

| Table | Holds |
|---|---|
| `grid_table` | Every namespace's tables: slug, label, what it `extends`, its declared indexes |
| `grid_column` | Their columns: key, type, cardinality, config, flags |
| `grid_row` | Every row of every table: `payload` (JSONB, by column key), `table_id`, `external_ref`, timestamps |
| `schema_version` | Which of grid's migrations ran, with each file's sha256 |

Each unique column and declared index becomes a real Postgres index on
`grid_row`: an expression index over the column's value, cast as its type
compares, partial on that table's live rows. Its name is
`gx_<table id>_<digest of what it covers>`, and Postgres's catalogue is the
record of which exist: every `apply` compares the two and builds or drops the
difference. A `ref` value is found from the target's side through the JSONB
GIN index on `payload`, so `on_delete` does not scan.

## Integrity

Postgres enforces what it can over one shared JSONB table: unique columns and
indexes, a column's cardinality, and the catalog's own keys. A value's type, a
required column and a `ref`'s target are checked by grid on every write, under
row locks: a write that names a `ref` target holds it `FOR SHARE` and archiving
a row takes it `FOR UPDATE` first, so a target cannot be archived while a write
that points at it is in flight. Each write also holds its namespace's schema
lock shared, and checks the table's spec again if it changed since the `Table`
was opened; a schema change (`apply`, `rename_column`, `migrate_to`) takes that
lock exclusively. So a change waits for the writes in flight, sees them when it
checks the existing rows, and the writes after it meet the new schema. Anything
that writes `grid_row` without grid bypasses those checks.

Rows, columns and tables are never deleted by grid: dropping one archives it
(`archived_at`), and every read skips archived ones. The one exception is a
dropped column's values: they stay in the rows until a column with the same key
is declared again, or another is renamed onto it, which discards them so the
new column starts empty.

*Proven by* `tests/stress/test_link_contention.py`, `tests/stress/test_schema_races.py` and `battle/s02`.

## Requirements

| | |
|---|---|
| **Python** | 3.14 |
| **PostgreSQL** | 14 or newer |
| **Driver** | `asyncpg`, through `forktex.database` (`postgresql+asyncpg://`) |
| **Privileges** | On first start, creating the `forktex_grid` schema; afterwards, owning it. Nothing else is read or written |
| **Extensions** | `pg_trgm`, only for the `fuzzy` filter and `trgm` indexes, installed in a schema on the search path. Grid does not create it; without it, both are refused with `BadRequestError` before anything is built or run |
| **Connections** | Direct, or pooled per session. `apply` and `apply_migrations` hold session-level advisory locks, which a pooler in transaction mode (PgBouncer `pool_mode=transaction`) cannot keep: point grid's engine at Postgres or at a session-mode pool. Behind one in transaction mode, a concurrent `apply` whose lock is released from another server connection raises `ServiceUnavailableError` saying so — the lock stays held on that server connection until the pooler closes it |

## Migrations

`apply_migrations(engine)` at startup. It takes an advisory lock on one
connection and migrates on another, so it needs a pool of at least two (with a
pool of one it times out at boot). Every replica can call it at once; it applies only what has not run, each file in one
transaction with its version row, and refuses to start if a file that already
ran has changed since. A version of grid that adds a migration ships it as a new
file, never an edit. An upgrade is a deploy: replicas booting a version with a
new migration, while a writer runs, apply it exactly once and keep every row
(`tests/contract/test_migrations.py`); twelve replicas booting together on an
empty database is `battle/s01`.

Grid's migrations are separate from yours. If you use Alembic, keep
autogenerate away from the `forktex_grid` schema (an `include_name` hook that
skips it), and do not wrap `apply_migrations` in an Alembic revision.

## Changing a schema on a populated deployment

`Namespace.apply` with the default `ApplyOptions(index_builds="concurrent")` is
the path for a namespace that has rows:

1. It **commits** your session, then takes an advisory lock for the namespace
   on a connection of its own, so two applies to one namespace run one after
   the other. It waits for that lock by polling, idle between tries, never
   inside a statement: a waiter holding a snapshot would deadlock with the
   `CONCURRENTLY` build of the apply ahead of it, in a cycle Postgres cannot see.
2. It writes the schema change and commits.
3. It builds each missing index with `CREATE INDEX CONCURRENTLY`, which does not
   block writes, on that connection. A build that fails is dropped and
   reported in `ApplyReport.indexes` — `failure: "duplicate_values"` when
   existing rows break a new unique index — and `complete` is `False`. The
   driver's message, which can quote row values, goes to the log only. Fix the
   data and apply again: every apply builds whatever index is missing.

So `apply` needs a session it may commit (`forktex.database.session()`), and two
connections from the pool while it runs: with a pool of one it waits for a
connection it can never get. Concurrent applies through one engine take turns
in the process, so a pool of two is always enough for them.

Two consequences of `CONCURRENTLY` to plan around:

- **It waits for every transaction older than itself**, in the whole database.
  A long transaction anywhere delays it; a client that holds a transaction open
  *while waiting for that apply* — another session of the same request, say —
  never lets it finish. Apply schemas from code paths that hold nothing else.
- **A failed unique index enforces nothing.** Until the next apply builds it,
  the column is declared unique in the catalog but writes are not refused.
  Check `report.complete`, and treat `False` as work to finish.

`index_builds="inline"` and `batch` build indexes inside your transaction,
holding a write lock on `grid_row` — every namespace's rows — until it commits.
Use them for new or empty tables. Grid makes those inline builds take turns
across namespaces, so two of them never deadlock each other; a transaction that
writes rows *and then* changes the schema can still deadlock with another doing
the same, which surfaces as `ConflictError`: roll back and retry.

*Proven by* `tests/stress/test_concurrency.py` and `battle/s06`.

## Performance

Every query is one SQL statement over `grid_row`, plus one `count(*)` when
`count` is asked and one query per embedded link. What decides its speed:

- **Filters on indexed columns use the index.** A comparison casts the stored
  JSON exactly as the column's index is built (`pg_cast`), and both carry the
  table's partial predicate, so Postgres plans an equality or range on a unique
  column or declared index onto that `gx_` index.
- **Everything else scans the table's live rows** through `ix_grid_row_table`.
  Declare an `IndexSpec` for the columns your screens filter and sort by.
- **Links** compile to `EXISTS` subqueries: a single link matches its target by
  `grid_row`'s primary key, a list link by JSONB containment. **Finding
  referrers** on archive uses the payload's GIN index.
- **Cursor paging** keeps every page as cheap as the first: the cursor's
  leading sort key bounds the index scan, so even the generic plan a prepared
  statement settles on starts at the cursor; ascending, the rows without a
  value are read after the rest, only once the page needs them. Offset paging
  reads and discards `offset` rows, and stops at 100 000.
- **Writes**: `create_many` normalises every row, checks all their `ref` values
  with one locked lookup per `ref` column, then inserts the rows in one batch.
  Every write call also takes the namespace's schema lock shared and reads the
  table's spec version: two small statements, which matter for one-row writes
  and not for batches.
- **Round trips**: over a slow network grid is bound by round trips, not by
  work, and a single-row write is several of them. Batch what you can.

As a guide to scale: on a laptop with Postgres 17 in Docker, 100 000 rows with
a `ref` and three indexes write through `create_many` at about 10 000 a second;
indexed lookups and counts answer in milliseconds, a count through a link in
tens of milliseconds, and a cursor walk over every row in seconds. At a million
rows, the cost of a cursor page stays flat from the first page to the last. As
for any Postgres table, size `shared_buffers` for the data.

*Proven by* `tests/stress/test_volume.py` and `battle/s07`.

## Schema changes and the rows already there

Grid keeps stored rows valid against their table's schema, as Postgres does for
its own columns:

- **A new column with a `default_value`** is filled in on every existing live
  row, like `ADD COLUMN … DEFAULT`.
- **A new required column without a default** is refused while the table has
  live rows: there is nothing to put in them.
- **Tightening a column** — making it required, taking options out of an enum,
  lowering a maximum length or a scale — is refused while any live row's value
  would break the new rule; the error says how many rows and names up to ten.
  Fix or archive them, then apply again.

`allowDestructive` is still required for those changes: it means a change may
make writes that used to succeed fail, or may drop data. It never lets the
stored rows contradict the schema. Checking reads the column's value from every
live row of that table, 1 000 at a time, inside the apply's transaction: on a
table of millions of rows, expect it to take seconds.

*Proven by* `tests/contract/test_schema_vs_data.py` and `battle/s06`.

## Reshaping a namespace

What `apply` will not change in place — a column's type or cardinality, which
table a link points at, two tables that should be one — `migrate_to` does by
copying: the namespace's live rows go into a new, empty namespace under the new
schema, in one transaction, and the source is only read. Then point your code
at the new namespace; archive the old one when you no longer need it
(`apply(Schema(), ApplyOptions(prune=True, allow_destructive=True))`).

- **It holds a write lock on `grid_row` until you commit.** The new tables'
  indexes are built inside the transaction, as a `batch`'s are, so every
  namespace's writes wait for the migration to finish; on a laptop it copies
  in the order of 10 000 linked rows a second. Migrate small namespaces at any
  time; schedule large ones.
- **Writes to the source wait until it commits.** It holds both namespaces'
  schema locks exclusively, so the copy is of the source as it stood, and the
  source's writers resume, unchanged, once the migration commits.
- **`dry_run` rehearses the whole copy and rolls it back,** reporting what
  would be carried and what would not (`columns_not_carried`,
  `tables_not_carried`, `links_dropped`). Run it first, read it, then migrate.

*Proven by* `tests/contract/test_migrate.py`, `tests/stress/test_schema_races.py` and `battle/s09`.

## Limits

What Postgres would refuse is refused before it reaches Postgres, as a
`BadRequestError` naming what was wrong:

| | Limit |
|---|---|
| Table slug, column key | 63 characters |
| A field type's id | 64 characters |
| Labels, `extends`, namespace | 255 characters; a namespace is not empty |
| `external_ref` | 255 characters |
| `integer` values | the signed 64-bit range |
| Any stored text, key or JSON string | no NUL character |
| A value in a unique column or index | small enough for a Postgres btree entry (about 2 700 bytes after compression) |

*Proven by* `tests/contract/test_limits.py` and `battle/s08`.

## Isolation

Every table belongs to one namespace, and every row lookup goes through its
table: a row, table or column of another namespace is `NotFoundError`,
indistinguishable from one that does not exist, and a `ref` value can only
name a row of the same namespace. What grid cannot know is whether the
namespace you pass is the right one: derive it in one place, from your
authentication, and never from a request parameter.

*Proven by* `tests/contract/test_isolation.py`, `battle/s03` and `battle/s08`.

## Runtime DDL

Applying a schema creates and drops indexes on `grid_row`, built from the data
you pass. Column keys are validated identifiers and every value is bound or
quoted by the dialect; nothing is interpolated into SQL. Letting untrusted input
declare columns and indexes is letting it add indexes to a table every
namespace shares — within limits grid enforces, but a cost you should decide to
pay.

## Failures and restarts

What happens when Postgres crashes mid-work, under load:

- **The call that was running fails** with SQLAlchemy's `DBAPIError` or
  `InterfaceError` — a lost connection, not a grid error, never masked by grid's
  own cleanup. Postgres rolls its transaction back; open a new session and retry.
- **No acknowledged write is lost**, and every stored row still validates
  against its table's model.
- **Rows, batches and migrations are all or nothing.** A batch killed before its
  commit leaves nothing; a migration killed halfway leaves its target namespace
  empty, and running it again copies every row.
- **An index build killed mid-way** leaves an invalid index in Postgres. The
  next `apply` of that namespace finds it, drops it and builds it again
  (`ApplyReport.indexes` lists it), after which uniqueness is enforced again.
  Until then the column's uniqueness is not enforced — the same as after a
  failed build.

Grid holds no state outside Postgres, so a restarted process needs nothing but
`apply_migrations` at boot, as always. Failing over to a replica is, for grid,
a restart against the promoted server: what survives is what Postgres's
replication kept. With synchronous replication that is every acknowledged
commit; with asynchronous replication the last ones may be lost, and grid adds
nothing to that trade-off.

*Proven by* `tests/stress/test_concurrency.py` and `battle/s10`.

## Logs

Grid logs through `forktex.log` under the `forktex_grid` logger and never
configures logging. Each apply that changes something logs its changes at
`INFO`; a failed concurrent index build logs at `WARNING`, with the driver's
message.
