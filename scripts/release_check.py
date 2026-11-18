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

import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _project() -> dict[str, str]:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]


def _declared_version() -> str:
    return str(_project()["version"])


def _changelog_sections(text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current: str | None = None
    for line in text.split("\n"):
        heading = re.match(r"^##\s+\[([^\]]+)\]", line)
        if heading:
            current = heading.group(1)
            sections[current] = ""
        elif current is not None:
            sections[current] += line + "\n"
    return sections


def _dist_problems(version: str) -> list[str]:
    dist = ROOT / "dist"
    if not dist.is_dir():
        return ["dist/ does not exist — run `fsd build`"]

    artefacts = sorted(p.name for p in dist.iterdir() if p.suffix in {".whl", ".gz"})
    if not artefacts:
        return ["dist/ is empty — run `fsd build`"]

    stale = [name for name in artefacts if f"-{version}-" not in name and f"-{version}." not in name]
    if stale:
        return [
            f"dist/ holds artefacts that are not version {version}: {', '.join(stale)}. "
            "Run `fsd build` — publishing uploads dist/ as it stands."
        ]
    return []


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False).stdout.strip()


def _tagged(version: str) -> bool:
    return f"v{version}" in _git("tag", "--points-at", "HEAD").split()


def _git_problems(version: str, *, tagged: bool) -> list[str]:
    # What is uploaded must be what is committed and tagged: a release built from a
    # dirty tree cannot be rebuilt from the repository afterwards.
    problems: list[str] = []
    if _git("status", "--porcelain"):
        problems.append("the working tree has uncommitted changes — commit them before publishing")
    if tagged and not _tagged(version):
        problems.append(f"HEAD is not tagged v{version} — run `fsd tag`")
    if not tagged and not _tagged(version) and _git("tag", "--list", f"v{version}"):
        problems.append(f"v{version} already tags another commit — raise the version")
    return problems


def check(*, tagged: bool = True) -> list[str]:
    version = _declared_version()
    problems: list[str] = []
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    sections = _changelog_sections(changelog)

    if version not in sections:
        problems.append(f"CHANGELOG.md has no '## [{version}]' section")
    elif not re.search(rf"^## \[{re.escape(version)}\] [—-] \d{{4}}-\d{{2}}-\d{{2}}$", changelog, re.M):
        problems.append(f"CHANGELOG.md's '## [{version}]' heading carries no date (YYYY-MM-DD)")

    unreleased = sections.get("Unreleased", "").strip()
    if unreleased:
        problems.append(
            "CHANGELOG.md still has entries under '## [Unreleased]'. Move them under "
            f"'## [{version}]' — a published version whose changes are filed as "
            "unreleased cannot be corrected afterwards."
        )

    problems.extend(_dist_problems(version))
    problems.extend(_git_problems(version, tagged=tagged))

    return problems


def _report(problems: list[str]) -> int:
    print("release-check FAILED:")
    for problem in problems:
        print(f"  - {problem}")
    return 1


def tag() -> int:
    version = _declared_version()
    if problems := check(tagged=False):
        return _report(problems)
    name = f"v{version}"
    if not _tagged(version):
        subprocess.run(["git", "tag", "-a", name, "-m", f"{_project()['name']} {version}"], cwd=ROOT, check=True)
    pushed = subprocess.run(["git", "push", "origin", name], cwd=ROOT, check=False).returncode
    if pushed:
        print(f"tagged {name} locally; pushing it failed — run `git push origin {name}`")
        return pushed
    print(f"tagged and pushed {name}: `fsd publish` uploads it")
    return 0


def main(argv: list[str]) -> int:
    if argv == ["tag"]:
        return tag()
    if problems := check():
        return _report(problems)
    print(f"release-check OK: {_declared_version()} is declared, dated, described, built and tagged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
