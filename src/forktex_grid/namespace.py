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

from sqlalchemy.ext.asyncio import AsyncSession

from forktex_grid.domain.batch import Batch
from forktex_grid.domain.limits import MAX_LABEL, storable_text
from forktex_grid.domain.migration import Migration
from forktex_grid.domain.schema import Schema
from forktex_grid.errors import BadRequestError
from forktex_grid.persist import catalog, indexes
from forktex_grid.persist.locks import schema_exclusive
from forktex_grid.persist.models import pg_schema_of
from forktex_grid.persist.tx import atomic
from forktex_grid.read.rows import row_counts
from forktex_grid.table import Table
from forktex_grid.write.apply import ApplyOptions, ApplyReport, apply_schema, lock_namespace
from forktex_grid.write.batch import BatchReport, apply_batch
from forktex_grid.write.migrate import MigrationReport, migrate


class Namespace:
    __slots__ = ("_name", "_session")

    def __init__(self, session: AsyncSession, namespace: str) -> None:
        if not namespace:
            raise BadRequestError("a namespace needs a name")
        self._session = session
        self._name = storable_text(namespace, "a namespace", max_length=MAX_LABEL)

    @property
    def namespace(self) -> str:
        return self._name

    async def describe(self) -> Schema:
        return await catalog.hydrate(self._session, self._name)

    async def apply(self, desired: Schema, options: ApplyOptions | None = None) -> ApplyReport:
        return await apply_schema(self._session, self._name, desired, options or ApplyOptions())

    async def batch(self, batch: Batch, options: ApplyOptions | None = None) -> BatchReport:
        return await apply_batch(self._session, self._name, batch, options or ApplyOptions())

    async def row_counts(self) -> dict[str, int]:
        return await row_counts(self._session, self._name)

    async def migrate_to(
        self, target: Namespace, migration: Migration, options: ApplyOptions | None = None
    ) -> MigrationReport:
        if target._session is not self._session:
            raise BadRequestError("a migration runs in one transaction: both namespaces need the same session")
        return await migrate(self._session, self._name, target.namespace, migration, options or ApplyOptions())

    async def table(self, slug: str) -> Table:
        return Table(self._session, await catalog.load_table(self._session, self._name, slug))

    async def rename_column(self, table: str, key: str, new_key: str) -> None:
        await lock_namespace(self._session, self._name)
        await schema_exclusive(self._session, self._name)
        ref = await catalog.load_table(self._session, self._name, table)
        async with atomic(self._session):
            await catalog.rename_column(self._session, ref.id, key, new_key)
            renamed = await catalog.load_table(self._session, self._name, table)
            await indexes.reconcile(self._session, pg_schema_of(self._session), ref.id, renamed.spec)


__all__ = ["Namespace"]
