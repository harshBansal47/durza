"""CI guard: no Alembic migration may DROP data, unless explicitly approved.

The history of this project contains a migration that misread a new model as a
rename and dropped the `conversions` table. Nobody should merge that shape
again without an explicit human decision, because once conversions hold real
revenue, the drop is unrecoverable.

Rule: the *upgrade* body of every migration in alembic/versions must not
contain `op.drop_table` or `op.drop_column`. Downgrade bodies are expected to
do exactly that, so they are ignored. Set OVERRIDE_DROP=1 in the environment
to approve a destructive deploy knowingly.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

VERSIONS = Path(__file__).resolve().parent.parent / "alembic" / "versions"

_BANNED = {"drop_table", "drop_column"}

# Historical exceptions, each allowed exactly once with a reason. New drops
# never join this list without a reviewed incident write-up.
_ALLOWLIST = {
    # Removed `ingestion_runs.skipped`, a column no code path ever wrote; the
    # pipeline never emitted it, so nothing was lost.
    "7b3f1a9c4d21_multi_source_core.py",
    # Password reset was removed from the product entirely (public sign-in is
    # Google + email codes). The table had one reader, the deleted endpoint; no
    # user data lived in it beyond a short-lived token hash. Dropping it keeps
    # the schema honest instead of leaving a dead table forever.
    "a4f1c8e2d9b7_passwordless_email_otp.py",
}


def _banned_calls(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(), filename=str(path))
    hits: list[tuple[int, str]] = []
    # Attribute calls like op.drop_table(...) only count inside upgrade().
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "upgrade":
            for child in ast.walk(node):
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr in _BANNED
                ):
                    hits.append((child.lineno, child.func.attr))
    return hits


def main() -> int:
    if os.environ.get("OVERRIDE_DROP") in {"1", "true", "yes"}:
        print("OVERRIDE_DROP set — destructive migrations allowed for this run.")
        return 0

    failures: list[str] = []
    for path in sorted(VERSIONS.glob("*.py")):
        if path.name in _ALLOWLIST:
            continue
        for lineno, attr in _banned_calls(path):
            failures.append(f"{path.name}:{lineno} upgrade() calls op.{attr}(...)")

    if failures:
        print("Destructive operations found in migration upgrade() bodies:")
        for failure in failures:
            print(f"  - {failure}")
        print(
            "\nIf this drop is intentional, re-run with OVERRIDE_DROP=1 and a reviewed "
            "deployment plan (and a pg_dump first)."
        )
        return 1
    print("OK: no drop_table/drop_column in any migration upgrade() body.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
