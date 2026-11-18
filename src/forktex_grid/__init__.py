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


from forktex.database.filters import SortDirection, SortKey

from forktex_grid.domain.batch import ArchiveRow, Batch, CreateRow, PatchRow, RowOp, UpsertRow
from forktex_grid.domain.enums import Cardinality, FieldType, IndexBuilds, OnDelete
from forktex_grid.domain.fieldtypes import (
    Capabilities,
    FieldTypeHandler,
    FilterOp,
    NoConfig,
    field_type,
    is_registered,
    register_field_type,
)
from forktex_grid.domain.migration import Migration, TableSource
from forktex_grid.domain.query import (
    And,
    Comparison,
    CursorPaging,
    Embed,
    Linked,
    LinkMatch,
    Not,
    OffsetPaging,
    Or,
    Query,
    col,
    link,
)
from forktex_grid.domain.row import Page, Row, ValueCount, ValueCounts
from forktex_grid.domain.row_model import RowValues
from forktex_grid.domain.schema import ColumnSpec, IndexSpec, Schema, TableSpec
from forktex_grid.errors import (
    AlreadyExistsError,
    BadRequestError,
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
)
from forktex_grid.namespace import Namespace
from forktex_grid.persist.migrations import apply_migrations
from forktex_grid.table import Table
from forktex_grid.write.apply import ApplyOptions, ApplyReport
from forktex_grid.write.batch import BatchReport
from forktex_grid.write.migrate import MigratedTable, MigrationReport

__all__ = [
    "AlreadyExistsError",
    "And",
    "ApplyOptions",
    "ApplyReport",
    "ArchiveRow",
    "BadRequestError",
    "Batch",
    "BatchReport",
    "Capabilities",
    "Cardinality",
    "ColumnSpec",
    "Comparison",
    "ConflictError",
    "CreateRow",
    "CursorPaging",
    "Embed",
    "FieldType",
    "FieldTypeHandler",
    "FilterOp",
    "IndexBuilds",
    "IndexSpec",
    "LinkMatch",
    "Linked",
    "MigratedTable",
    "Migration",
    "MigrationReport",
    "Namespace",
    "NoConfig",
    "Not",
    "NotFoundError",
    "OffsetPaging",
    "OnDelete",
    "Or",
    "Page",
    "PatchRow",
    "Query",
    "Row",
    "RowOp",
    "RowValues",
    "Schema",
    "ServiceUnavailableError",
    "SortDirection",
    "SortKey",
    "Table",
    "TableSource",
    "TableSpec",
    "UpsertRow",
    "ValueCount",
    "ValueCounts",
    "apply_migrations",
    "col",
    "field_type",
    "is_registered",
    "link",
    "register_field_type",
]
