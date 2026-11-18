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

import importlib

import forktex_grid

PUBLIC = {
    "ValueCounts",
    "ValueCount",
    "TableSource",
    "MigrationReport",
    "Migration",
    "MigratedTable",
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
    "TableSpec",
    "UpsertRow",
    "apply_migrations",
    "col",
    "field_type",
    "is_registered",
    "link",
    "register_field_type",
}


def test_the_public_surface_is_frozen() -> None:
    assert set(forktex_grid.__all__) == PUBLIC


def test_every_public_name_resolves() -> None:
    assert all(hasattr(forktex_grid, name) for name in forktex_grid.__all__)


def test_the_agent_tools_resolve() -> None:
    ops = importlib.import_module("forktex_grid.agent")
    assert {"READ_TOOLS", "TOOLS", "run", "tool_schemas"} <= set(ops.__all__)
