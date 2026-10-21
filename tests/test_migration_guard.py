"""The production guard on Alembic commands.

A mistake here is a production write, so the rules are pinned by test rather
than by convention:

* `upgrade` in production needs `ALLOW_PROD_MIGRATION=1` for that one run;
* `downgrade` and `stamp` in production are refused with **no** override;
* read-only commands are always allowed;
* outside production nothing is restricted.
"""

from __future__ import annotations

import pytest

from app.core.migration_guard import (
    DOWNGRADE,
    READ_ONLY,
    STAMP,
    UNKNOWN,
    UPGRADE,
    enforce,
    enforce_from_environment,
    resolve_command,
)


class TestResolveCommand:
    def test_reads_the_command_from_the_command_line(self) -> None:
        assert resolve_command(["alembic", "upgrade", "head"]) == UPGRADE
        assert resolve_command(["alembic", "downgrade", "-1"]) == DOWNGRADE
        assert resolve_command(["alembic", "stamp", "head"]) == STAMP
        assert resolve_command(["alembic", "current"]) == READ_ONLY
        assert resolve_command(["alembic", "check"]) == READ_ONLY
        assert resolve_command(["alembic", "heads"]) == READ_ONLY

    def test_skips_flags_and_their_values(self) -> None:
        assert resolve_command(["alembic", "-x", "env=prod", "upgrade", "head"]) == UPGRADE
        assert resolve_command(["alembic", "--name", "my_db", "downgrade", "base"]) == DOWNGRADE

    def test_falls_back_to_the_alembic_function(self) -> None:
        # A programmatic invocation has no useful argv.
        assert resolve_command([], "upgrade") == UPGRADE
        assert resolve_command([], "do_stamp") == STAMP
        # `heads` and `check` share one function; both are read-only.
        assert resolve_command([], "retrieve_migrations") == READ_ONLY

    def test_an_unknown_command_is_not_treated_as_safe(self) -> None:
        assert resolve_command(["alembic", "something-new"]) == UNKNOWN
        assert resolve_command([], "") == UNKNOWN


class TestProductionGuard:
    def test_upgrade_without_the_override_is_refused(self) -> None:
        with pytest.raises(RuntimeError, match="ALLOW_PROD_MIGRATION"):
            enforce(UPGRADE, environment="production", allow_prod_migration=None)

    def test_upgrade_with_the_override_is_allowed(self) -> None:
        enforce(UPGRADE, environment="production", allow_prod_migration="1")

    def test_upgrade_with_a_wrong_override_value_is_refused(self) -> None:
        with pytest.raises(RuntimeError, match="ALLOW_PROD_MIGRATION"):
            enforce(UPGRADE, environment="production", allow_prod_migration="true")

    @pytest.mark.parametrize("command", [DOWNGRADE, STAMP])
    def test_destructive_commands_are_refused_even_with_the_override(self, command: str) -> None:
        # No override exists for these, on purpose: a production schema is
        # restored from a restore point, not by a command.
        with pytest.raises(RuntimeError, match="no override"):
            enforce(command, environment="production", allow_prod_migration="1")

    @pytest.mark.parametrize("command", [DOWNGRADE, STAMP])
    def test_destructive_commands_are_refused_without_the_override(self, command: str) -> None:
        with pytest.raises(RuntimeError, match="no override"):
            enforce(command, environment="production", allow_prod_migration=None)

    @pytest.mark.parametrize("command", [READ_ONLY])
    def test_read_only_commands_are_allowed(self, command: str) -> None:
        enforce(command, environment="production", allow_prod_migration=None)

    def test_an_unknown_command_is_refused(self) -> None:
        with pytest.raises(RuntimeError, match="unidentified"):
            enforce(UNKNOWN, environment="production", allow_prod_migration="1")


class TestOutsideProduction:
    @pytest.mark.parametrize("command", [UPGRADE, DOWNGRADE, STAMP, READ_ONLY, UNKNOWN])
    def test_nothing_is_restricted_outside_production(self, command: str) -> None:
        enforce(command, environment="development", allow_prod_migration=None)
        enforce(command, environment="test", allow_prod_migration=None)

    def test_the_environment_is_read_from_the_process(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ALLOW_PROD_MIGRATION", raising=False)
        with pytest.raises(RuntimeError):
            enforce_from_environment(UPGRADE, "production")
        monkeypatch.setenv("ALLOW_PROD_MIGRATION", "1")
        enforce_from_environment(UPGRADE, "production")
        with pytest.raises(RuntimeError):
            enforce_from_environment(DOWNGRADE, "production")
