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

from pathlib import Path
from typing import TYPE_CHECKING

from forktex.database.migrate import SchemaMigrationRunner

from forktex_grid.persist.models import SCHEMA

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


def _migrations_dir() -> Path:
    return Path(__file__).resolve().parent


async def apply_migrations(engine: AsyncEngine, schema: str = SCHEMA) -> None:
    runner = SchemaMigrationRunner(engine=engine, schema=schema, migrations_dir=_migrations_dir())
    await runner.apply()


__all__ = ["apply_migrations"]
