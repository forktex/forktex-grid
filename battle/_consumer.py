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

import json
import os
import random
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

import forktex_grid

if "site-packages" not in Path(forktex_grid.__file__).parts:
    raise SystemExit(f"forktex_grid must come from the installed wheel, not {forktex_grid.__file__}")

DATABASE_URL = os.environ["DATABASE_URL"]
SEED = int(os.environ.get("BATTLE_SEED", "0"))


def rng(salt: str = "") -> random.Random:
    return random.Random(f"{SEED}:{salt}")


def engine(pool_size: int = 10) -> AsyncEngine:
    return create_async_engine(DATABASE_URL, pool_size=pool_size, max_overflow=0, pool_timeout=60)


@asynccontextmanager
async def session_of(bind: AsyncEngine) -> AsyncIterator[AsyncSession]:
    maker = async_sessionmaker(bind=bind, expire_on_commit=False)
    async with maker() as s:
        yield s


class Report:
    def __init__(self) -> None:
        self.facts: dict[str, Any] = {}
        self.started = time.perf_counter()

    def count(self, key: str, by: int = 1) -> None:
        self.facts[key] = self.facts.get(key, 0) + by

    def set(self, key: str, value: Any) -> None:
        self.facts[key] = value

    def timed(self, key: str, since: float) -> None:
        self.facts[key] = round(time.perf_counter() - since, 3)

    def emit(self) -> None:
        self.facts["total_s"] = round(time.perf_counter() - self.started, 2)
        print("REPORT " + json.dumps(self.facts, default=str), flush=True)


def check(condition: bool, message: str) -> None:
    if not condition:
        print(f"INVARIANT BROKEN (seed {SEED}): {message}", file=sys.stderr, flush=True)
        raise SystemExit(1)
