"""Production guard for Alembic commands.

The API is reachable from a laptop, a CI runner and a deploy host, and
`alembic/env.py` resolves the database URL from the same environment the app
uses. That means an `alembic downgrade` typed in the wrong place is a
production write, and nothing in the tool stops it.

The rules are deliberately asymmetric:

* `downgrade` and `stamp` are **always** refused in production. There is no
  override for them, on purpose: a downgrade is the one command whose damage is
  proportional to how far it goes, and "I meant to do it" is exactly the
  reasoning that should not be able to unlock it. A production schema is
  restored from a restore point, not by a command.
* `upgrade` is allowed in production **only** when `ALLOW_PROD_MIGRATION=1` is
  present in the environment of that one run. The flag is passed per run (the
  deploy step puts it on the one-off migration container with `-e`), so it
  never has to sit in a `.env` where a later, unrelated run would pick it up.
* Everything else — `current`, `check`, `history`, `heads`, `branches`,
  `version`, `revision` — is read-only with respect to the database and is
  allowed, because those are the commands an operator needs when they are
  trying to find out what state the database is in.

The check happens before the connection is opened, so a refused command costs
nothing and touches nothing.
"""

from __future__ import annotations

import os

#: Commands that can change the shape of the database.
UPGRADE = "upgrade"
DOWNGRADE = "downgrade"
STAMP = "stamp"

#: A command that only reads.
READ_ONLY = "read-only"

#: A command this guard could not identify. Treated as unsafe.
UNKNOWN = "unknown"

#: Refused in production with no override of any kind.
ALWAYS_REFUSED = frozenset({DOWNGRADE, STAMP})

#: Allowed in production only with ALLOW_PROD_MIGRATION=1.
REQUIRES_OVERRIDE = frozenset({UPGRADE})

#: The environment variable that unlocks a single approved production upgrade.
UPGRADE_OVERRIDE_ENV = "ALLOW_PROD_MIGRATION"

#: What alembic's own command functions are called, mapped to what they do.
#:
#: alembic wires the function that implements a command into the environment
#: context, but two read-only commands (`heads` and `check`) share one of them,
#: so this mapping is deliberately coarse: everything that is not a mutating
#: command is "read-only".
_COMMAND_FUNCTIONS = {
    "upgrade": UPGRADE,
    "downgrade": DOWNGRADE,
    "do_stamp": STAMP,
    "show_current": READ_ONLY,
    "edit_current": READ_ONLY,
    "_display_current_history": READ_ONLY,
    "retrieve_migrations": READ_ONLY,
    "display_version": READ_ONLY,
    "do_ensure_version": READ_ONLY,
    "nothing": READ_ONLY,
}

#: Command names alembic's CLI accepts, and whether each one is safe.
_CLI_COMMANDS = {
    "upgrade": UPGRADE,
    "downgrade": DOWNGRADE,
    "stamp": STAMP,
    "current": READ_ONLY,
    "check": READ_ONLY,
    "history": READ_ONLY,
    "heads": READ_ONLY,
    "branches": READ_ONLY,
    "version": READ_ONLY,
    "revision": READ_ONLY,
    "edit": READ_ONLY,
    "merge": READ_ONLY,
    "init": READ_ONLY,
    "list": READ_ONLY,
}

#: Flags that take a value, so the value is not mistaken for the command.
_VALUE_FLAGS = frozenset(
    {
        "-x",
        "--name",
        "-c",
        "--config",
        "--rev-id",
        "--version-path",
        "--label",
        "--branch-label",
        "--depends-on",
    }
)


def resolve_command(argv: list[str], function_name: str = "") -> str:
    """Which alembic command is running.

    The command line is the reliable source: alembic's CLI puts the command
    first, and it is what a human or a deploy step actually typed. The function
    name is the fallback for a programmatic invocation.
    """
    skip_next = False
    for arg in argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if arg in _VALUE_FLAGS:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        return _CLI_COMMANDS.get(arg, UNKNOWN)
    return _COMMAND_FUNCTIONS.get(function_name, UNKNOWN)


def enforce(
    command: str,
    *,
    environment: str,
    allow_prod_migration: str | None = None,
) -> None:
    """Raise when `command` must not run in `environment`.

    Called from `alembic/env.py` before the engine is created, so a refusal
    happens before any connection is opened.
    """
    if environment != "production":
        return

    if command in ALWAYS_REFUSED:
        raise RuntimeError(
            f"Refusing to run `alembic {command}` against production. "
            "Destructive migrations have no override: a production schema is "
            "restored from a restore point, not by a command. "
            f"({UPGRADE_OVERRIDE_ENV}=1 unlocks `alembic upgrade` only.)"
        )

    if command in REQUIRES_OVERRIDE:
        if allow_prod_migration != "1":
            raise RuntimeError(
                f"Refusing to run `alembic {command}` against production without "
                f"{UPGRADE_OVERRIDE_ENV}=1 in the environment of this run. Pass it "
                "per run (the deploy step sets it on the one-off migration "
                "container with `-e`); do not leave it in a .env."
            )
        return

    if command == UNKNOWN:
        raise RuntimeError(
            "Refusing to run an unidentified alembic command against production. "
            "The command could not be determined, so it is treated as unsafe."
        )


def enforce_from_environment(command: str, environment: str) -> None:
    """`enforce`, reading the override from the process environment."""
    enforce(
        command,
        environment=environment,
        allow_prod_migration=os.getenv(UPGRADE_OVERRIDE_ENV),
    )
