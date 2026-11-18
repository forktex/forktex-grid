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

import uuid
from datetime import datetime

import sqlalchemy as sa
from forktex.database.models import TimestampMixin, substrate_base
from forktex.types import JsonObject, JsonValue
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from forktex_grid.domain.limits import MAX_LABEL, MAX_NAME, MAX_TYPE_ID

# A schema name in every deployed consumer database: never renamed, or their data is orphaned.
SCHEMA = "forktex_grid"

_Base = substrate_base(SCHEMA)
_LIVE = sa.text("archived_at IS NULL")


def _fk(target: str) -> sa.ForeignKey:
    return sa.ForeignKey(f"{SCHEMA}.{target}", ondelete="CASCADE")


class GridTable(_Base, TimestampMixin):
    __tablename__ = "grid_table"
    __table_args__ = (sa.Index("uq_grid_table_slug", "namespace", "slug", unique=True, postgresql_where=_LIVE),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid7)
    namespace: Mapped[str] = mapped_column(sa.String(MAX_LABEL))
    slug: Mapped[str] = mapped_column(sa.String(MAX_NAME))
    label: Mapped[str] = mapped_column(sa.String(MAX_LABEL))
    extends: Mapped[str | None] = mapped_column(sa.String(MAX_LABEL))
    indexes: Mapped[list[JsonObject]] = mapped_column(JSONB, default=list)
    archived_at: Mapped[datetime | None]


class GridColumn(_Base, TimestampMixin):
    __tablename__ = "grid_column"
    __table_args__ = (
        sa.Index("uq_grid_column_key", "table_id", "key", unique=True, postgresql_where=_LIVE),
        sa.CheckConstraint("cardinality IN ('one', 'many')", name="ck_grid_column_cardinality"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid7)
    table_id: Mapped[uuid.UUID] = mapped_column(_fk("grid_table.id"))
    key: Mapped[str] = mapped_column(sa.String(MAX_NAME))
    label: Mapped[str] = mapped_column(sa.String(MAX_LABEL))
    type_id: Mapped[str] = mapped_column(sa.String(MAX_TYPE_ID))
    cardinality: Mapped[str] = mapped_column(sa.String(8))
    is_required: Mapped[bool]
    is_unique: Mapped[bool]
    config: Mapped[JsonObject] = mapped_column(JSONB, default=dict)
    default_value: Mapped[JsonValue] = mapped_column(JSONB, nullable=True)
    display_order: Mapped[int]
    archived_at: Mapped[datetime | None]


class GridRow(_Base, TimestampMixin):
    __tablename__ = "grid_row"
    __table_args__ = (
        sa.Index("ix_grid_row_table", "table_id", postgresql_where=_LIVE),
        sa.Index(
            "ix_grid_row_payload", "payload", postgresql_using="gin", postgresql_ops={"payload": "jsonb_path_ops"}
        ),
        sa.Index(
            "uq_grid_row_external_ref",
            "table_id",
            "external_ref",
            unique=True,
            postgresql_where=sa.text("external_ref IS NOT NULL AND archived_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid7)
    table_id: Mapped[uuid.UUID] = mapped_column(_fk("grid_table.id"))
    external_ref: Mapped[str | None] = mapped_column(sa.Text)
    payload: Mapped[JsonObject] = mapped_column(JSONB, default=dict)
    archived_at: Mapped[datetime | None]


def pg_schema_of(session: AsyncSession) -> str:
    options = session.get_bind().get_execution_options()
    return (options.get("schema_translate_map") or {}).get(SCHEMA, SCHEMA)


__all__ = ["SCHEMA", "GridColumn", "GridRow", "GridTable", "pg_schema_of"]
