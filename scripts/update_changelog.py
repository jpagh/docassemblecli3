"""Rename the [Unreleased] section in CHANGELOG.md to the new version."""

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"
PYPROJECT = ROOT / "pyproject.toml"


def get_new_version() -> str:
    """Return the version to release.

    Prefer the ``BVHOOK_NEW_VERSION`` environment variable injected by
    bump-my-version, falling back to ``project.version`` in pyproject.toml for
    manual runs.
    """
    if version := os.getenv("BVHOOK_NEW_VERSION"):
        return version

    import tomllib

    data = PYPROJECT.read_text(encoding="utf-8")
    return tomllib.loads(data)["project"]["version"]


def main() -> int:
    new_version = get_new_version().strip()

    if "-" in new_version:
        print(f"Pre-release {new_version}; skipping CHANGELOG update")
        return 0

    today = datetime.now(tz=UTC).date().isoformat()
    unreleased_header = "## Unreleased"

    changelog = CHANGELOG.read_text(encoding="utf-8")

    if unreleased_header not in changelog:
        print("No [Unreleased] section found in CHANGELOG.md", file=sys.stderr)
        return 1

    versioned_header = f"## [{new_version}] - {today}"

    changelog = changelog.replace(unreleased_header, versioned_header, 1)

    pos = changelog.find(versioned_header)
    changelog = changelog[:pos] + f"{unreleased_header}\n\n" + changelog[pos:]

    CHANGELOG.write_text(changelog, encoding="utf-8")

    print(f"CHANGELOG: [Unreleased] -> [{new_version}] - {today}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
