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
from collections.abc import Mapping, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from forktex_grid.domain.query import Query, Where
from forktex_grid.domain.resolved import ResolvedTable
from forktex_grid.domain.row import ExternalRef, Page, Row, ValueCounts, external_ref_text
from forktex_grid.domain.row_model import RowValues, row_model
from forktex_grid.domain.schema import TableSpec
from forktex_grid.read import rows as read_rows
from forktex_grid.read.query import count_by, count_rows, run_query
from forktex_grid.write import rows as write_rows


class Table:
    __slots__ = ("_model", "_ref", "_session")

    def __init__(self, session: AsyncSession, ref: ResolvedTable) -> None:
        self._session = session
        self._ref = ref
        self._model: type[RowValues] | None = None

    @property
    def slug(self) -> str:
        return self._ref.spec.slug

    @property
    def namespace(self) -> str:
        return self._ref.namespace

    @property
    def spec(self) -> TableSpec:
        return self._ref.spec

    def model(self) -> type[RowValues]:
        if self._model is None:
            self._model = row_model(self._ref.spec)
        return self._model

    def typed(self, row: Row) -> RowValues:
        return self.model().model_validate(row.values)

    async def create(self, values: Mapping[str, object], *, external_ref: ExternalRef | None = None) -> Row:
        return (await write_rows.create(self._session, self._ref, [values], [external_ref]))[0]

    async def create_many(
        self, values: Sequence[Mapping[str, object]], *, external_refs: Sequence[ExternalRef | None] | None = None
    ) -> list[Row]:
        refs = list(external_refs) if external_refs is not None else [None] * len(values)
        return await write_rows.create(self._session, self._ref, values, refs)

    async def get(self, row_id: uuid.UUID) -> Row:
        return await read_rows.get(self._session, self._ref, row_id)

    async def patch(self, row_id: uuid.UUID, *, values: Mapping[str, object]) -> Row:
        return await write_rows.patch(self._session, self._ref, row_id, values)

    async def archive(self, row_id: uuid.UUID) -> None:
        await write_rows.archive(self._session, self._ref, row_id)

    async def get_by_external_ref(self, external_ref: ExternalRef) -> Row | None:
        found = await read_rows.by_external_ref(self._session, self._ref, [external_ref])
        return found.get(external_ref_text(external_ref) or "")

    async def get_many_by_external_ref(self, external_refs: Sequence[ExternalRef]) -> dict[str, Row]:
        return await read_rows.by_external_ref(self._session, self._ref, external_refs)

    async def upsert_by_external_ref(self, external_ref: ExternalRef, *, values: Mapping[str, object]) -> Row:
        return await write_rows.upsert(self._session, self._ref, external_ref, values)

    async def query(self, query: Query | None = None) -> Page:
        return await run_query(self._session, self._ref, query or Query())

    async def count(self, where: Where | None = None) -> int:
        return await count_rows(self._session, self._ref, where)

    async def count_by(self, column: str, where: Where | None = None, limit: int = 100) -> ValueCounts:
        return await count_by(self._session, self._ref, column, where, limit)


__all__ = ["Table"]
