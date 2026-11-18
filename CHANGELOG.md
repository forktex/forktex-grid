# Changelog

`forktex-grid` follows [Semantic Versioning](https://semver.org/): every name in `forktex_grid.__all__`,
the schema and query documents, the agent tool names and the `forktex_grid` database schema are covered.

## [Unreleased]

## [1.0.1] — 2026-11-18

No module changed; the package's code is the same as 1.0.0.

- The repository moved to [github.com/forktex/forktex-grid](https://github.com/forktex/forktex-grid); the package's links and the README point there.

## [1.0.0] — 2026-11-18

First stable release. Requires Python 3.14, PostgreSQL 14+, `forktex>=1.2,<2`, SQLAlchemy 2.1.6, asyncpg 0.32 and pydantic 2.14 or newer.

- `Namespace`: declare tables with `apply`, read them back with `describe`, write a schema and rows together with `batch`, rename a column, count rows, and move a namespace into a new schema with `migrate_to`.
- `Table`: create, read, patch, archive and upsert rows, look them up by a host key, query them, count them in total or per value, and read them as typed Python values.
- `Query`: filters, links one level deep, embedded linked rows, sorting, offset or cursor pages and counts, written as JSON or with `col()` and `link()`.
- Ten built-in field types, links between tables with `restrict`, `cascade` or `set_null`, and your own field types from any pydantic type.
- Unique columns and declared indexes are real Postgres indexes, and filters use them.
- A grid table can extend one of your own tables, one row per host row.
- Every namespace is isolated: another tenant's table or row does not exist.
- Existing rows stay valid when a schema changes, also under concurrent writes.
- `forktex_grid.agent`: the same operations as tools for an LLM agent, with JSON schemas and a read-only set.
- Errors are `forktex.error`'s, so a FastAPI app renders them as usual.

### Known limitations

- A column's type, a link's target table and what a table extends cannot change in place; use `migrate_to`.
- `migrate_to` holds writes to every namespace until it commits.
- A list (`many`) column cannot be filtered, sorted or indexed.
- Queries follow links one level deep and count, but do not otherwise aggregate or join your own tables.
- PgBouncer in transaction mode is not supported, and `apply` needs a pool of at least two connections.
