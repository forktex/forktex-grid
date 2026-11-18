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

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORK = REPO / ".battle"
PASSWORD = "battle"
CONSUMER_EXTRAS = ["fastapi>=0.115", "uvicorn>=0.30", "httpx>=0.27"]


def sh(*cmd: str, cwd: Path | None = None, capture: bool = False) -> str:
    done = subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=capture)
    return done.stdout.strip() if capture else ""


def build_wheel() -> Path:
    sh("make", "build", cwd=REPO)
    (wheel,) = sorted((REPO / "dist").glob("forktex_grid-*-py3-none-any.whl"))
    return wheel


def fresh_venv(wheel: Path) -> Path:
    venv = WORK / "venv"
    shutil.rmtree(venv, ignore_errors=True)
    sh("uv", "venv", "--quiet", "--python", "3.14", str(venv))
    python = venv / "bin" / "python"
    sh("uv", "pip", "install", "--quiet", "--python", str(python), str(wheel), *CONSUMER_EXTRAS)
    return python


def start_postgres(version: str) -> tuple[str, int]:
    name = f"grid-battle-{os.getpid()}"
    sh(
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "--tmpfs",
        "/var/lib/postgresql/data",
        "-e",
        f"POSTGRES_PASSWORD={PASSWORD}",
        "-p",
        "127.0.0.1::5432",
        f"postgres:{version}-alpine",
        "-c",
        "max_connections=400",
        "-c",
        "fsync=off",
        "-c",
        "shared_buffers=256MB",
    )
    port = int(sh("docker", "port", name, "5432/tcp", capture=True).rsplit(":", 1)[1])
    for _ in range(60):
        ready = subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres"], capture_output=True)
        if ready.returncode == 0:
            time.sleep(1)
            return name, port
        time.sleep(0.5)
    raise SystemExit("postgres did not start")


def create_database(container: str, database: str) -> None:
    sh("docker", "exec", container, "psql", "-U", "postgres", "-q", "-c", f'CREATE DATABASE "{database}"')
    sh("docker", "exec", container, "psql", "-U", "postgres", "-q", "-d", database, "-c", "CREATE EXTENSION pg_trgm")


def run_scenario(python: Path, script: Path, container: str, port: int, database: str, seed: int) -> dict[str, object]:
    env = {
        **os.environ,
        "BATTLE_POSTGRES_CONTAINER": container,
        "DATABASE_URL": f"postgresql+asyncpg://postgres:{PASSWORD}@127.0.0.1:{port}/{database}",
        "BATTLE_SEED": str(seed),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    started = time.perf_counter()
    done = subprocess.run([str(python), str(script)], cwd=script.parent, env=env, text=True, capture_output=True)
    elapsed = round(time.perf_counter() - started, 1)
    report_line = next((line for line in reversed(done.stdout.splitlines()) if line.startswith("REPORT ")), None)
    return {
        "scenario": script.stem,
        "ok": done.returncode == 0,
        "seconds": elapsed,
        "report": json.loads(report_line[7:]) if report_line else None,
        "stderr": done.stderr[-4000:] if done.returncode else "",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build grid, install it fresh, and run every consumer scenario.")
    parser.add_argument("only", nargs="*", help="Scenario name prefixes to run (default: all)")
    parser.add_argument("--seed", type=int, default=int(time.time()))
    parser.add_argument("--postgres", default="17", help="Postgres major version to run against")
    args = parser.parse_args()

    WORK.mkdir(exist_ok=True)
    scripts = sorted(
        p for p in (REPO / "battle").glob("s[0-9][0-9]_*.py") if not args.only or p.stem.startswith(tuple(args.only))
    )
    python = fresh_venv(build_wheel())
    container, port = start_postgres(args.postgres)
    results = []
    try:
        for script in scripts:
            database = script.stem.lower()
            create_database(container, database)
            result = run_scenario(python, script, container, port, database, args.seed)
            results.append(result)
            mark = "PASS" if result["ok"] else "FAIL"
            print(f"{mark} {script.stem:40s} {result['seconds']:7.1f}s  {json.dumps(result['report'])}", flush=True)
            if not result["ok"]:
                print(result["stderr"], file=sys.stderr)
    finally:
        subprocess.run(["docker", "stop", container], capture_output=True)
    (WORK / "report.json").write_text(json.dumps({"seed": args.seed, "results": results}, indent=2))
    failed = [r["scenario"] for r in results if not r["ok"]]
    print(
        f"postgres {args.postgres}, seed {args.seed}: {len(results) - len(failed)}/{len(results)} scenarios passed"
        + (f"; failed: {failed}" if failed else "")
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
