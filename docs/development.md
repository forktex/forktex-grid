# Developing `forktex-grid`

For working on the library itself. Everything a consumer needs is in the
[README](../README.md), the [guide](guide.md) and the [reference](reference.md);
nothing here is part of the public contract.

## Layout

Layers, each a directory whose
name says what it does; imports point one way, down the table:

| Layer | Modules | Holds |
|---|---|---|
| — | `__init__.py`, `errors.py` | The public surface (`__all__` is the contract); the errors grid raises |
| `domain/` | `enums`, `fieldtypes`, `builtin_types`, `limits`, `validation`, `schema`, `diff`, `query`, `batch`, `migration`, `row`, `row_model`, `resolved`, `values` | The vocabulary, the four documents and the value rules. **No I/O**: it imports no SQLAlchemy and no driver |
| `persist/` | `models`, `migrations/`, `catalog`, `indexes`, `casts`, `extensions`, `locks`, `tx` | What is stored and how: the ORM tables, migrations, the catalog of tables and columns, derived indexes, the one SQL cast per type, the `pg_trgm` check, the namespace locks, the transaction boundary and the rehearsal (a dry run that executes and rolls back) |
| `read/` | `rows`, `fields`, `compile`, `query` | Loading rows and counting them per table; a `Query` compiled to one statement and run; counts per value |
| `write/` | `links`, `rows`, `conform`, `apply`, `batch`, `migrate` | Link integrity and archive cascades; row writes; keeping existing rows valid through a schema change; schema convergence; batches; carrying a namespace into a new schema |
| facade | `table.py`, `namespace.py` | `Table` and `Namespace`, the public classes |
| tools | `agent.py` | The agent tools, outside `__all__` |

Import-linter holds two contracts in `fsd lint`: the layers import one way
(`domain` → `persist` → `read` → `write` → `table` → `namespace` → `agent`), and
`domain` imports neither `sqlalchemy` nor `asyncpg`.

The import contract keeps the layers free of cycles.

## The model

A namespace has tables; a table has typed columns and indexes; a row is a JSONB
payload keyed by column. Grid's whole store is `grid_table` (with its indexes as
JSON), `grid_column` and `grid_row`. A link is a `ref` column — its value the
target's id, its config the target table and `on_delete` — so it has one source
of truth, and "who points here" is a containment query the payload's GIN index
serves.

`Namespace.apply` is the one way a schema changes: hydrate, merge, check, diff,
refuse what is destructive unless allowed, write the catalog, reconcile indexes.
`batch` runs it inline. `rename_column` is the one change outside it: a diff
cannot tell a rename from a drop and an add.

Indexes are derived, not recorded: `persist/indexes.py` computes what a spec
needs, names each `gx_<table id>_<digest>`, and compares with Postgres's
catalogue — inline under one global advisory lock (a transactional build locks
the shared `grid_row`), or `CONCURRENTLY` on an AUTOCOMMIT connection. A filter
and an index cast a value through the same function (`persist/casts.py`, from
the type's `pg_cast`), which is why the planner can use one for the other.

A `Query` compiles in `read/`: `fields.py` makes a column or system field one
`QueryField` (compared expression, text form, typed literal, cursor value), so
filters, sorts and keyset cursors treat both alike; `compile.py` walks the
`where` tree, hands each comparison to forktex's `compile_comparison` and turns
a link into an `EXISTS`; `query.py` pages, counts and loads embeds.

Where an invariant lives: one object about itself, including its type and
config, in the spec's validators (a spec that constructs is valid, and its
config is canonical); across tables in `Schema.check`; against data in
`domain/values.py` and `write/links.py` on every write, and in `write/conform.py`
when a schema change meets rows that already exist.

## Conventions

- **No prose in code.** `src/`, `tests/`, `battle/` and `scripts/` carry no
  docstrings and no comments, except the note on the `forktex_grid` schema
  literal, which deployed databases depend on. Facts go in these pages. The
  agent tools' descriptions are data (`json_schema_extra`), because a model
  reads them. `examples/assistant.py` keeps a module docstring: how to run it.
- **Models.** What is stored or crosses the wire — specs, configs, reports,
  rows, row ops, tool inputs — is a `forktex.types.BaseWireValueObject`,
  camelCase and frozen; internal values are `BaseValueObject`s. No bare
  `BaseModel`.
- **forktex first.** Identifiers, sessions, savepoints, integrity and read
  boundaries, the filter language, `Page`, cursors and the keyset predicate,
  advisory locks and the migration runner come from `forktex.database`.
  `persist/tx.atomic` composes them with a savepoint and one boundary of its
  own, which turns Postgres's "too large to index" (54000) into a
  `BadRequestError`.
- **Errors** are `forktex.error`'s; driver text never reaches a caller — a
  failed index build is a `failure` code, its message is logged. A lost
  connection is not grid's to translate: it reaches the caller as SQLAlchemy
  raised it, never masked by grid's own cleanup.
- **SQL** is constructs, not strings, with typed bound values
  (`persist/casts.py`); `pg_cast` is the one source of a type's SQL expression
  for filters, sorts and indexes; upsert is a locked lookup then
  `INSERT … ON CONFLICT DO NOTHING`.
- **One configuration object.** `ApplyOptions` holds the only switches grid
  has: how a schema changes. Reads and writes have no modes: the JSON or Python
  form of a value is chosen by the call (`row.values`, `table.typed(row)`).
- **A capability has a consumer**: a host's tenant fields, an agent's own
  tables, a runtime-declared table.
- **Tests** exercise behaviour through the public surface against a real
  Postgres; nothing reads `src/` or `docs/`. The coverage floor is the measured
  figure, floored, and only goes up.
- **One source for the agent tools**: they are derived from the `Namespace` and
  `Table` method signatures — a tool's name, parameters and result type are the
  method's. Only the descriptions, which the code cannot state, are data.

What Postgres cannot hold for grid, and how grid compensates:

- **Per-table constraints.** A value's type, a required column and a `ref`'s
  target live in one shared JSONB table, where Postgres cannot hold per-table
  rules. Grid checks them on every write, and makes the `ref` check safe under
  concurrency with row locks (`FOR SHARE` on targets written, `FOR UPDATE` on a
  row before it is archived). Uniqueness is in the schema.
- **Row-comparison keysets.** Sort keys are nullable payload values, so keyset
  paging uses `forktex.database.keyset_predicate`, which handles nulls, rather
  than one row comparison.

## Design decisions

**The namespace lock polls.** `forktex.database.advisory_lock` waits inside a
statement, so the waiter holds a snapshot; `CREATE INDEX CONCURRENTLY` in the
holder waits for every older snapshot, and the two wait on each other through
the client, where Postgres's deadlock detector cannot see. `persist/locks.py`
polls `pg_try_advisory_lock` from an idle autocommit connection instead. It
moves to forktex once a second package needs a lock it can hold across a
`CONCURRENTLY` build.

**Writes share the schema lock.** Every row write takes its namespace's schema
lock shared (a transaction-level advisory lock) and compares the table's spec
version (`catalog.spec_version`: the table's and its columns' timestamps and
column count) with the one its `Table` was opened at, reloading the spec when
they differ. The catalog phase of `apply`, `rename_column` and `migrate_to`
(source and target) take it exclusively. Without it a write could pass checks
against a spec a concurrent apply was tightening, or slip past
`ensure_rows_fit` uncommitted. The exclusive lock is held only while the catalog
changes, never across a `CONCURRENTLY` build, so writers wait milliseconds, not
for an index.

**Concurrent applies take turns per engine.** A concurrent apply holds two
connections (the lock's and the session's); several in one process could each
hold one and wait for a second until the pool times out. `engine_turn` makes
them take turns in the process; across processes, each has its own pool.

**A lock released from elsewhere is reported.** `polled_advisory_lock` checks
that `pg_advisory_unlock` found its lock: behind a pooler in transaction mode
the unlock lands on another server connection and finds nothing. It raises
`ServiceUnavailableError` then (after the work has succeeded; an error inside
the block is raised as it is), because that lock is leaked until the pooler
closes the connection. On a connection that was lost there is nothing to
release — a session lock ends with its connection — so the loss is raised.

**Migrations write past the ORM.** `write/migrate.py` validates each copied row
with `stored_payload`, then inserts in bulk and sets deferred links with one
`UPDATE … payload || links` per chunk. Every link value it writes names a row
it just wrote (re-pointed through its id map, the rest dropped and counted), so
the per-row reference check would only repeat what holds by construction, at
many times the cost, leaving stale objects in the caller's session. Tables are
filled by strongly connected components of the link graph, targets first;
inside a cycle, required links first.

**Dry runs that execute.** `batch` and `migrate_to` run inside
`persist/tx.rehearsal` (a savepoint rolled back), taking their locks inside it
so the rollback releases them; `apply`'s dry run only plans.

**Cursor pages bound their scan.** The keyset predicate is an `OR` that
includes `key IS NULL`, which no index range can serve; under the generic plan
asyncpg's prepared statements settle on, each page would scan from the start.
`read/compile.leading_bound` adds the bound the predicate implies
(`key >= cursor`, `<=` descending), and for an ascending nullable key
`null_tail` reads the rows without a value in a second statement, only when the
page is not yet full.

**Agent tool shapes.** A batch row's `op` is required in the schema because the
union is told apart by it; the Python default only serves constructors.
`_typed_columns` writes one column variant per field type with its own config,
so a model writes an `enum` with its `options` the first time, and generated
titles are dropped from every schema, because each token reaches the model on
every request. `migrate_to` is not a tool because it names a second namespace.

## What proves what

Every claim the consumer docs make names what proves it: a guide chapter ends
in *Proven by*, a statement in [operating](operating.md) about load, failure or
scale names its battle scenario. Two kinds of proof, at two cadences:

- **`tests/`, on every build** (`fsd test`): behaviour through the public
  surface, against a real Postgres, from the source tree.
- **`battle/`, before every release** (`fsd battle`): consumer programs
  against the **built wheel** installed into a new virtualenv, on a new
  Postgres in Docker, each scenario in its own database. Nothing from the
  source tree or the dev environment is used.

A new capability lands with all of its sides: a guide chapter, a usage or
contract test, and — when it is about concurrency, volume or failure — a battle
scenario that says so. A doc claim
with nothing behind it is either given a check or taken out.

### Tests

Four layers, and every test runs on every build — nothing is optional or marked
slow:

| Directory | Proves | Reads like |
|---|---|---|
| `tests/usage/` | The guide's stories work end to end | One test per thing a consumer does |
| `tests/contract/` | Each public call does what the [reference](reference.md) says, and refuses what it should | One file per area: apply, rows, refs, extension, query (where, links, pages, select), counts per value, typed values, types, limits, `pg_trgm`, schema changes against existing rows, batch, namespace migration, agent tools, isolation, grid's own migrations, the public surface |
| `tests/properties/` | Invariants over generated inputs (hypothesis, bounded) | A query returns exactly the rows an independent three-valued-logic oracle selects, and `count_by` the groups it computes; cursor and offset pages visit every row once in one order; a random schema applied converges and re-applies to an empty plan; storing a value is idempotent and the typed model round-trips it |
| `tests/stress/` | Behaviour under contention, volume and lost connections | Writers racing an archive under each `onDelete` leave no live row pointing at an archived one; applies take turns; a schema change and writes in flight see each other; a migration holds its source's writes; a connection lost mid index build is raised and the next apply rebuilds; volume within time budgets; a unique-column filter planned onto grid's index |

The time budgets in `tests/stress/test_volume.py` are an order of magnitude
above a laptop's figures: they fail on a per-row round trip or a lost index,
not on a slow machine. The oracle in `tests/properties/test_query_oracle.py`
encodes SQL's rules, including that membership in an empty list is false even
for a null; inputs worth keeping are pinned as `@example`s or contract tests.

### Battle scenarios

```bash
python battle/run.py                    # every scenario, a seed from the clock
python battle/run.py s02 s05 --seed 7   # some scenarios, reproducibly
python battle/run.py --postgres 14      # the oldest Postgres grid supports
BATTLE_STEPS=20000 BATTLE_CALLS=40000 BATTLE_SECONDS=180 BATTLE_MIGRATE_ROWS=10000 python battle/run.py s02 s05 s06 s09
BATTLE_ROWS=1000000 python battle/run.py s07
```

`run.py` needs Python 3.14, `uv` and Docker. Each scenario prints one line with
its measurements; `.battle/report.json` keeps them.

| Scenario | A consumer that | Holds | Backs |
|---|---|---|---|
| `s01_cold_start_replicas` | boots 12 replicas at once on an empty database, all applying one schema and racing for one unique value | one migration, one winner, no invalid index, a second boot changes nothing | [operating: migrations](operating.md#migrations) |
| `s02_model_based_soak` | runs a long random workload — creates, patches, upserts, archives with cascades, rollbacks, queries — on a CRM-shaped schema | grid always equals an in-memory model of what it should hold (`BATTLE_STEPS`) | [guide: link rows](guide.md#link-rows), [query language](query.md) |
| `s03_saas_http_tenants` | serves 25 tenants over HTTP with 100 concurrent clients, an attacker and a body fuzzer | no 5xx, no cross-tenant read, counts equal listings | [guide: web app](guide.md#run-inside-a-web-app), [operating: isolation](operating.md#isolation) |
| `s04_host_table_extension` | extends its own 20 000-row table and races upserts on shared keys | one row per host key; every value was written by someone; list screens and host joins agree | [guide: tenant fields](guide.md#add-tenant-fields-to-a-table-you-already-own) |
| `s05_agent_fuzz` | drives the agent tools with random, mostly malformed calls (`BATTLE_CALLS`), then as a read-only agent | only grid errors escape; every stored row validates against its table's model; a read-only agent is refused every write and changes nothing | [guide: agents](guide.md#give-an-agent-its-own-tables) |
| `s06_schema_evolution_under_load` | changes the schema of a populated table while writers and readers run (`BATTLE_SECONDS`) | no hang or deadlock; rows still fit; no invalid index; unique columns unique | [operating: changing a schema](operating.md#changing-a-schema-on-a-populated-deployment), [schema changes and rows](operating.md#schema-changes-and-the-rows-already-there) |
| `s07_scale_100k` | writes and reads `BATTLE_ROWS` rows (100 000) | the figures in [performance](operating.md#performance); `EXPLAIN` shows grid's indexes used; a late cursor page costs no more than an early one | [operating: performance](operating.md#performance) |
| `s08_hostile_input_and_isolation` | uses SQL-shaped and unicode names and values, and reaches for another tenant's rows | every reach refused, every name confined, a sentinel table intact | [operating: limits, isolation, runtime DDL](operating.md#limits) |
| `s09_knowledge_migrations` | migrates a random linked knowledge base (self links, lists, cycles, archived rows) as it is, renamed through and through, then with two tables merged (`BATTLE_MIGRATE_ROWS`) | after each, every row by label holds the same values, links, external ref and timestamps; counts per value equal; each dry run reported exactly what the migration then did | [guide: reshape](guide.md#reshape-a-namespace), [operating: reshaping](operating.md#reshaping-a-namespace) |
| `s10_crash_recovery` | kills Postgres's client backends (`SIGKILL`, crash recovery) during a concurrent index build with writers running, during a migration, and during an uncommitted batch | the crash surfaces as a SQLAlchemy connection error; no acknowledged write is lost; the next apply rebuilds the interrupted index and uniqueness holds; the migration and the batch left nothing | [operating: failures and restarts](operating.md#failures-and-restarts) |

**A live model.** [`examples/assistant.py`](../examples/assistant.py) runs the
agent tools under a real LLM (OpenAI, `OPENAI_API_KEY`). It is not in `run.py` —
its answers are not repeatable — but run it before a release that changes a tool
or its description, and read what the model gets wrong.

## Commands

Every command is bound in [`fsd.toml`](../fsd.toml) and run by `fsd`
([forktex-fsd](https://github.com/forktex/forktex-fsd)), installed once with
`pipx install forktex-fsd` or `uv tool install forktex-fsd`:

```bash
fsd setup           # poetry install --with dev
fsd                 # every atom, its binding and compliance
fsd assurance       # format, lint and import contracts, parse + types, tests at the floor (needs Docker), licence headers
fsd security        # lock check and dependency audit
fsd documentation   # the manual's files exist
fsd build           # wheel + sdist, twine check, the installed wheel carries its migrations
fsd tidy            # rewrite formatting, fixable lint and licence headers
fsd test -k query   # one atom; what follows goes to pytest
fsd battle          # every consumer scenario against the built wheel, on Postgres 14 and 17
```

Run `fsd build` on its own: a concurrent run races over `dist/`. Tests share one
Postgres container per session; each test gets its own schema, brought up by the
real migration runner and mapped over `forktex_grid` with `schema_translate_map`.
`tests/conftest.py`'s `declare(ns, *tables)` is the setup most tests need, and
`tests/contract/query_tables.py` holds the tables the query tests share.

## Conventions

- Python 3.14; PEP 695 generics and PEP 758 `except A, B:` are in use.
- `__all__` is frozen by `tests/contract/test_public_surface.py`.
- No `assert`, `noqa` or `type: ignore` in `src/`.
- Ids are `uuid.uuid7()`, as the column default.
- `forktex_grid` is a Postgres schema name in deployed databases; never renamed.
- A shipped migration is never edited: `tests/contract/test_migration_immutability.py`
  pins what the installed package ships.
- Annotations built at runtime (a column's `Literal` of options, `list[...]`,
  `... | None`) go through the named builders in `domain/fieldtypes.py`, typed
  `TypeForm`, so `src/` keeps its no-`Any` rule without suppressions.
- A guide file prints what its comment says: `print(x)  # <exactly the output>`,
  any explanation on the line above.

## Release

1. Move the `[Unreleased]` entries of `CHANGELOG.md` under the new version, dated.
2. Bump `version` in `pyproject.toml`.
3. The gate and `fsd build`: it builds `dist/` and installs the wheel into a clean venv.
4. `fsd battle`: every scenario passes against the built wheel, on the oldest
   and newest supported Postgres (14 and 17).
5. If a tool or its description changed, a session with `examples/assistant.py`.
6. Commit and push, then `fsd tag`: it tags `HEAD` `v<version>` and pushes the tag.
7. `fsd rehearse`, install from TestPyPI into a clean venv, then `fsd publish`.
   Both refuse a dirty tree, an untagged `HEAD`, an undated changelog section or
   stale artefacts in `dist/`.
