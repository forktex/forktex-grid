# AGENTS.md

`forktex-grid` is a library: tables, columns and rows declared at
runtime and stored in PostgreSQL, over the `forktex` substrate. It has no CLI
and no service of its own.

For consumers: [`README.md`](README.md), then [`docs/guide.md`](docs/guide.md),
[`docs/reference.md`](docs/reference.md) and [`docs/operating.md`](docs/operating.md).

For working on the library: [`docs/development.md`](docs/development.md) — the
architecture (three tables, one apply engine, indexes derived from the spec),
layout, commands and conventions. Read it before changing anything.

The gate is `fsd assurance`, `fsd security` and `fsd documentation`, bound in
[`fsd.toml`](fsd.toml); the tests need Docker. Before a release, `fsd battle`
runs the consumer scenarios against the built wheel. Publishing is manual: a
person runs `fsd tag`, then `fsd publish`.

`forktex_grid` is also a Postgres schema name in deployed databases. It is never
renamed.
