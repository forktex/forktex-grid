# Security Policy

## Reporting a vulnerability

Email **info@forktex.com** with details and, where possible, a reproduction. Please do
not open a public issue for an unfixed vulnerability.

## Supported versions

| Version | Supported |
| --- | --- |
| 1.0.x | ✅ |

## Scope

`forktex-grid` is a library. It has no CLI, no daemon and no network listener, and it
runs entirely inside the consuming service's process under that service's privileges.
It talks to exactly one piece of infrastructure — the Postgres connection you hand it.

The security-relevant surface is narrow, and one part of it is unusual enough to state
plainly:

- **Runtime DDL.** This is the one thing grid does that an ordinary library does not.
  Applying a schema creates and drops indexes on grid's shared `grid_row` table, driven by
  data a consumer supplies. Column keys are validated identifiers, every value is bound or
  quoted by the dialect's preparer, and no statement is assembled by string interpolation.
  **A consumer that lets untrusted input declare columns and indexes is granting it that
  authority** — bound it, or don't pass it through.
- **Query compilation.** Filters and sorts compile through `forktex.database.filters`
  against a capability-gated column set: an operator a field type does not declare is
  refused rather than mistranslated, and values are bound, never stringified.
- **The JSONB payload.** Row values are stored as given, after the field type's
  `normalize`. Grid does not encrypt them and does not know which are sensitive.

## What this library does not do

- **No phone-home.** Nothing here contacts a ForkTex service. The only traffic is to
  the Postgres you configure.
- **No credential storage.** The connection is supplied by the consumer; grid holds no
  secret of its own and has no key material.
- **No authentication or authorization.** These are the consuming service's concern.
  `namespace` is a plain string and tenant isolation is by convention: grid enforces
  that a query stays inside the namespace it was given, not that the caller was
  entitled to that namespace.
- **No encryption of stored values.** If a column holds personal data, the consuming
  service owns that obligation.

## Operator responsibilities

- **Treat schema declaration as privileged.** `apply`, `batch` and the `ops` tool
  surface all change the database's shape. The `ops` module exists for LLM
  tool-calling; a model driving it has the same authority the caller grants it, so
  bound it to a namespace you are willing to see rewritten. Grid never reads or writes
  your own tables, so neither can a model.
- **`allow_destructive` is a real switch.** A reconcile that drops or tightens columns
  is refused without it. Pair it with `dry_run=True` and read the report first.
- **Transport security.** Use a TLS-enabled connection URL. The library does not
  downgrade or override your transport settings.
- **Dependency currency.** `fsd audit` runs `pip-audit` against the resolved
  environment and is part of the gate, as `fsd security`.

## Licensing

Dual-licensed: AGPL-3.0-or-later, or a commercial license from FORKTEX S.R.L. See
[LICENSE](LICENSE) and [NOTICE](NOTICE).
