# Copyright (C) 2026 FORKTEX S.R.L.
#
# SPDX-License-Identifier: AGPL-3.0-or-later OR LicenseRef-ForkTex-Commercial
#
# This file is part of ForkTex Grid.
#
# For commercial licensing -- including use in proprietary products, SaaS
# deployments, or any context where AGPL obligations cannot be met -- you
# MUST obtain a commercial license from FORKTEX S.R.L. (info@forktex.com).
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.


from __future__ import annotations

import hashlib
import uuid
from typing import Any, Literal

import sqlalchemy as sa
from forktex.database import ddl
from forktex.database.identifiers import validate_schema
from forktex.database.integrity import sqlstate
from forktex.database.locks import advisory_key, xact_lock
from forktex.database.models import UtcTimestamp
from forktex.log import get_logger
from forktex.types import BaseWireValueObject
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from forktex_grid.domain.fieldtypes import get_field_type
from forktex_grid.domain.schema import IndexSpec, TableSpec
from forktex_grid.persist.casts import as_compared

logger = get_logger(__name__)

_UNIQUE_VIOLATION = "23505"

PHYSICAL_DDL_LOCK = advisory_key("grid.physical-ddl")

_PG_CLASS = sa.table("pg_class", sa.column("oid"), sa.column("relname"), sa.column("relnamespace"))
_PG_NAMESPACE = sa.table("pg_namespace", sa.column("oid"), sa.column("nspname"))
_PG_INDEX = sa.table("pg_index", sa.column("indexrelid"), sa.column("indisvalid"))


class IndexOutcome(BaseWireValueObject):
    table: str
    index: str
    columns: tuple[str, ...]
    name: str
    ok: bool
    failure: Literal["duplicate_values", "build_failed"] | None = None


def _grid_row(schema: str) -> sa.Table:
    return sa.Table(
        "grid_row",
        sa.MetaData(),
        sa.Column("table_id", sa.UUID(as_uuid=True)),
        sa.Column("payload", JSONB),
        sa.Column("archived_at", UtcTimestamp),
        schema=schema,
    )


def _prefix(table_id: uuid.UUID) -> str:
    return f"gx_{table_id.hex}_"


def wanted(spec: TableSpec) -> list[IndexSpec]:
    out = {
        i.signature: i for i in (IndexSpec(column_keys=(c.key,), is_unique=True) for c in spec.columns if c.is_unique)
    }
    out.update((i.signature, i) for i in spec.indexes)
    return list(out.values())


def build(schema: str, table_id: uuid.UUID, spec: TableSpec, index: IndexSpec, *, concurrently: bool) -> sa.Index:
    validate_schema(schema)
    row = _grid_row(schema)
    exprs: list[sa.ColumnElement[Any]] = []
    ops: dict[str, str] = {}
    casts: list[str] = []
    for i, key in enumerate(index.column_keys):
        text = row.c.payload[key].astext
        if index.kind == "trgm":
            exprs.append(text.label(f"c{i}"))
            ops[f"c{i}"] = "gin_trgm_ops"
            casts.append("text")
        else:
            handler = get_field_type(spec.column(key).type_id)
            exprs.append(as_compared(handler, text))
            casts.append(handler.pg_cast or "text")
    digest = hashlib.sha256(f"{index.signature}|{','.join(casts)}".encode()).hexdigest()[:16]
    return sa.Index(
        _prefix(table_id) + digest,
        *exprs,
        unique=index.is_unique,
        postgresql_using="gin" if index.kind == "trgm" else "btree",
        postgresql_ops=ops,
        postgresql_where=sa.and_(row.c.table_id == table_id, row.c.archived_at.is_(None)),
        postgresql_concurrently=concurrently,
    )


def _drop(schema: str, name: str, *, concurrently: bool) -> ddl.DropIndex:
    return ddl.DropIndex(
        sa.Index(name, _grid_row(schema).c.table_id, postgresql_concurrently=concurrently), if_exists=True
    )


async def existing(executor: AsyncSession | AsyncConnection, schema: str, table_id: uuid.UUID) -> dict[str, bool]:
    rows = await executor.execute(
        sa.select(_PG_CLASS.c.relname, _PG_INDEX.c.indisvalid)
        .join(_PG_NAMESPACE, _PG_NAMESPACE.c.oid == _PG_CLASS.c.relnamespace)
        .join(_PG_INDEX, _PG_INDEX.c.indexrelid == _PG_CLASS.c.oid)
        .where(_PG_NAMESPACE.c.nspname == schema, _PG_CLASS.c.relname.startswith(_prefix(table_id), autoescape=True))
    )
    return {name: valid for name, valid in rows}


def _plan(
    schema: str, table_id: uuid.UUID, spec: TableSpec | None, have: dict[str, bool], *, concurrently: bool
) -> tuple[list[str], list[tuple[IndexSpec, sa.Index]]]:
    want: dict[str, tuple[IndexSpec, sa.Index]] = {}
    if spec is not None:
        for index in wanted(spec):
            construct = build(schema, table_id, spec, index, concurrently=concurrently)
            want[str(construct.name)] = (index, construct)
    drops = [name for name, valid in have.items() if name not in want or not valid]
    creates = [(spec_, construct) for name, (spec_, construct) in want.items() if not have.get(name, False)]
    return drops, creates


async def reconcile(session: AsyncSession, schema: str, table_id: uuid.UUID, spec: TableSpec | None) -> None:
    drops, creates = _plan(schema, table_id, spec, await existing(session, schema, table_id), concurrently=False)
    if not drops and not creates:
        return
    await xact_lock(session, PHYSICAL_DDL_LOCK)
    for name in drops:
        await session.execute(_drop(schema, name, concurrently=False))
    for _, construct in creates:
        await session.execute(ddl.CreateIndex(construct, if_not_exists=True))


async def reconcile_concurrently(
    conn: AsyncConnection, schema: str, table_id: uuid.UUID, slug: str, spec: TableSpec | None
) -> list[IndexOutcome]:
    drops, creates = _plan(schema, table_id, spec, await existing(conn, schema, table_id), concurrently=True)
    outcomes: list[IndexOutcome] = []
    for name in drops:
        await conn.execute(_drop(schema, name, concurrently=True))
    for index, construct in creates:
        outcome = IndexOutcome(
            table=slug, index=index.signature, columns=index.column_keys, name=str(construct.name), ok=True
        )
        try:
            await conn.execute(ddl.CreateIndex(construct, if_not_exists=True))
        except DBAPIError as exc:
            if conn.invalidated:
                raise
            logger.warning("grid: building index %s of %r failed", index.signature, slug, exc_info=True)
            await conn.execute(_drop(schema, outcome.name, concurrently=True))
            unique_violation = sqlstate(exc) == _UNIQUE_VIOLATION
            failure = "duplicate_values" if unique_violation else "build_failed"
            outcome = outcome.model_copy(update={"ok": False, "failure": failure})
        outcomes.append(outcome)
    return outcomes


__all__ = ["PHYSICAL_DDL_LOCK", "IndexOutcome", "build", "existing", "reconcile", "reconcile_concurrently", "wanted"]
