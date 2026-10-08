"""The migrations must be reversible.

`alembic downgrade base && alembic upgrade head` failing is not a papercut. It
means a deploy cannot be rolled back and then re-applied, so the only recovery
from a bad migration is to restore a database from backup — which for a
production database is an outage measured in how stale the backup is.

The specific failure this file exists to prevent is the one that actually
happened here. Alembic does not track enum types declared inline inside
`op.create_table`: `op.drop_table` removes the column that used the type and
leaves the type behind. Every downgrade therefore completed "successfully" while
leaving nine types in the database, and the next upgrade died on the first one:

    type "ad_position" already exists

A downgrade that reports success and leaves the schema unusable is worse than one
that fails loudly, because it is believed.

These tests are static — they parse the migration files — so they run in
milliseconds and need no database. The dynamic half of this property, that the
migrations actually apply and roll back against a real Postgres, is the
`migrate-check` CI job and `scripts/migration_roundtrip.py`.
"""

import ast
import re
from pathlib import Path

VERSIONS = Path(__file__).resolve().parent.parent / "alembic" / "versions"


def _migration_files() -> list[Path]:
    return sorted(p for p in VERSIONS.glob("*.py") if not p.name.startswith("__"))


def _called_name(node: ast.expr) -> str | None:
    """`sa.Enum` -> "Enum"; `postgresql.ENUM` -> "ENUM"; anything else -> None."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _enum_names(path: Path) -> set[str]:
    """Enum type names the `upgrade()` half of a migration creates.

    Found by walking for actual `Enum(...)` calls rather than by grepping
    `name="..."`. Grepping picks up every other `name=` in the file — unique
    constraints are declared as `sa.UniqueConstraint(..., name="uq_saved_coupon")`
    and are dropped automatically with their table — and then reports them as
    missing types, which trains you to ignore this test.

    Both spellings are covered: `sa.Enum(..., name="x")` inline in a column, and
    `sa.Enum(...).create(bind, checkfirst=True)` called on its own line.
    """
    source = path.read_text()
    tree = ast.parse(source)
    upgrade = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade"),
        None,
    )
    if upgrade is None:
        return set()

    found: set[str] = set()
    for node in ast.walk(upgrade):
        if not isinstance(node, ast.Call):
            continue
        if _called_name(node.func) not in ("Enum", "ENUM"):
            continue
        for keyword in node.keywords:
            if (
                keyword.arg == "name"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            ):
                found.add(keyword.value.value)
    return found


def _dropped_type_names(path: Path) -> set[str]:
    source = path.read_text()
    tree = ast.parse(source)
    downgrade = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "downgrade"
        ),
        None,
    )
    if downgrade is None:
        return set()
    segment = ast.get_source_segment(source, downgrade) or ""
    # `DROP TYPE IF EXISTS x` and `DROP TYPE x` both count; the point is that
    # the type is named at all.
    return set(re.findall(r"DROP TYPE(?:\s+IF EXISTS)?\s+([a-z_]+)", segment))


def test_there_are_migrations_to_check() -> None:
    # A glob that silently matches nothing would make every other test in this
    # file vacuously pass.
    files = _migration_files()
    assert files, f"no migration files found under {VERSIONS}"


def test_every_migration_has_an_upgrade_and_a_downgrade() -> None:
    for path in _migration_files():
        source = path.read_text()
        assert "def upgrade()" in source, f"{path.name} has no upgrade()"
        assert "def downgrade()" in source, f"{path.name} has no downgrade()"


def test_every_enum_a_migration_creates_is_dropped_by_its_downgrade() -> None:
    """The bug, stated as an invariant.

    Each type is checked against the downgrade of the *same* migration that
    creates it. Checking against the union of all downgrades would pass while
    leaving the ordering wrong — the type has to be gone before the migration
    that follows it tries to create it again.
    """
    offenders: dict[str, set[str]] = {}
    for path in _migration_files():
        created = _enum_names(path)
        if not created:
            continue
        dropped = _dropped_type_names(path)
        missing = created - dropped
        if missing:
            offenders[path.name] = missing

    assert not offenders, (
        "enum types created but never dropped:\n"
        + "\n".join(f"  {name}: {sorted(types)}" for name, types in offenders.items())
        + "\n\n`op.drop_table` does not drop a native type. Without an explicit"
        " DROP TYPE, the next `alembic upgrade head` fails with"
        " `<type> already exists` and the migration chain cannot be re-applied."
    )


def test_the_downgrade_drops_types_after_dropping_the_tables_that_use_them() -> None:
    """Ordering, not just presence.

    `DROP TYPE` fails with "cannot drop type X because other objects depend on
    it" if a column still uses it. A downgrade that names the right types in the
    wrong order fails at exactly the moment it is needed — during an incident.
    """
    for path in _migration_files():
        source = path.read_text()
        tree = ast.parse(source)
        downgrade = next(
            (
                node
                for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "downgrade"
            ),
            None,
        )
        if downgrade is None or "DROP TYPE" not in (
            ast.get_source_segment(source, downgrade) or ""
        ):
            continue

        segment = ast.get_source_segment(source, downgrade) or ""
        last_table_drop = max(
            (match.end() for match in re.finditer(r"drop_table\(", segment)),
            default=-1,
        )
        first_type_drop = min(
            (match.start() for match in re.finditer(r"DROP TYPE", segment)),
            default=len(segment),
        )
        assert last_table_drop < first_type_drop, (
            f"{path.name}: a DROP TYPE comes before the last drop_table(). "
            "Postgres refuses to drop a type a live column still depends on."
        )


def test_the_enum_drop_statements_do_not_interpolate_anything() -> None:
    """`DROP TYPE` takes a literal here, never a formatted value.

    The names are fixed by the migration that owns them, so there is nothing to
    interpolate — and a literal removes any question of where a value could come
    from. An `f"..."` with no placeholder is dead weight; an `f"..."` whose
    placeholder is not visible here is a migration executing something the
    reviewer cannot see.
    """
    for path in _migration_files():
        source = path.read_text()
        for line in source.splitlines():
            if "DROP TYPE" in line and 'f"' in line:
                pytest_fail = (
                    f"{path.name}: {line.strip()} — DROP TYPE must be a plain string literal"
                )
                raise AssertionError(pytest_fail)
