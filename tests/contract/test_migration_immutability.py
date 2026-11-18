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
from importlib.resources import files

SHIPPED = {
    "v0001__schema.sql": "036b8bb6ce4fccf19f9f19e3990ee9450a27eae20ae14f64d78f73f8551b5406",
}


def _shipped() -> dict[str, bytes]:
    migrations = files("forktex_grid.persist.migrations")
    return {p.name: p.read_bytes() for p in migrations.iterdir() if p.name.endswith(".sql")}


def test_every_migration_the_package_ships_is_pinned_and_unchanged() -> None:
    digests = {name: hashlib.sha256(content).hexdigest() for name, content in _shipped().items()}
    assert digests == SHIPPED
