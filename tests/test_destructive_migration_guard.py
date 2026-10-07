"""D6: destructive-migration guard tests.

The guard (`scripts/check_destructive_migrations.py`) must fail CI
when any migration's `upgrade()` body drops a table or column, and
must only yield when `OVERRIDE_DROP` is set or the migration is on
the documented allow-list.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_destructive_migrations.py"
VERSIONS = Path(__file__).resolve().parent.parent / "alembic" / "versions"


def _load_guard():
    spec = importlib.util.spec_from_file_location("check_destructive_migrations", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def guard(monkeypatch, tmp_path):
    """The guard module pointed at a scratch versions directory."""
    module = _load_guard()
    monkeypatch.delenv("OVERRIDE_DROP", raising=False)
    module.VERSIONS = tmp_path
    return module


def _write_migration(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(f'"""Migration."""\n\nimport op\n\n\n{body}\n')
    return path


def test_guard_passes_on_the_real_history():
    """The committed migration history contains no un-approved drops."""
    assert _load_guard().main() == 0


def test_guard_fails_on_drop_table_in_upgrade(guard, tmp_path):
    _write_migration(
        tmp_path,
        "aaa1111_bad.py",
        "def upgrade():\n    op.drop_table('conversions')\n\n\ndef downgrade():\n    pass",
    )
    assert guard.main() == 1


def test_guard_fails_on_drop_column_in_upgrade(guard, tmp_path):
    _write_migration(
        tmp_path,
        "aaa1111_bad.py",
        "def upgrade():\n    op.drop_column('coupons', 'legacy_flag')\n\n\ndef downgrade():\n    pass",
    )
    assert guard.main() == 1


def test_guard_ignores_drops_in_downgrade(guard, tmp_path):
    """Downgrades are expected to restore the dropped schema."""
    _write_migration(
        tmp_path,
        "aaa1111_ok.py",
        "def upgrade():\n    op.create_table('conversions')\n\n\ndef downgrade():\n    op.drop_table('conversions')",
    )
    assert guard.main() == 0


def test_guard_ignores_bare_names_not_attribute_calls(guard, tmp_path):
    """A local function named drop_table is not an Alembic op call."""
    _write_migration(
        tmp_path,
        "aaa1111_ok.py",
        "def upgrade():\n    drop_table('scratch')\n\n\ndef downgrade():\n    pass",
    )
    assert guard.main() == 0


def test_override_drop_env_bypasses_guard(guard, tmp_path, monkeypatch):
    _write_migration(
        tmp_path,
        "aaa1111_bad.py",
        "def upgrade():\n    op.drop_table('conversions')\n\n\ndef downgrade():\n    pass",
    )
    monkeypatch.setenv("OVERRIDE_DROP", "1")
    assert guard.main() == 0


def test_allowlisted_migration_is_skipped(guard, tmp_path):
    _write_migration(
        tmp_path,
        "7b3f1a9c4d21_multi_source_core.py",
        "def upgrade():\n    op.drop_column('ingestion_runs', 'skipped')\n\n\ndef downgrade():\n    pass",
    )
    assert guard.main() == 0


def test_guard_reports_file_and_line(guard, tmp_path, capsys):
    _write_migration(
        tmp_path,
        "aaa1111_bad.py",
        "def upgrade():\n    op.create_table('x')\n    op.drop_table('conversions')\n\n\ndef downgrade():\n    pass",
    )
    guard.main()
    out = capsys.readouterr().out
    # Body starts at line 6, so the drop call is on line 8.
    assert "aaa1111_bad.py:8" in out
    assert "op.drop_table" in out


def test_real_versions_directory_exists():
    """The guard only works if the real versions directory is where it expects."""
    assert VERSIONS.is_dir()
    assert os.environ.get("OVERRIDE_DROP") != "1"
