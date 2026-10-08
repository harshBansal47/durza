"""Every model module must be listed in `alembic/env.py`.

Two real bugs came from an omission here, and they are the reason this file
exists rather than a one-line assertion in a migration test:

1. `conversions` was missing from `env.py`, so autogenerate could not see the
   table. A migration was generated from a database that happened to still hold
   it, and that migration's `upgrade()` **dropped the table**. The test suite
   never noticed, because the suite runs on SQLite and creates its schema from
   `Base.metadata` rather than from migrations — so the one thing that would
   have caught it was structurally incapable of catching it.
2. `alembic check` reported the database as clean *while the drift was present*,
   because the models it was not importing were not part of the comparison.

The failure mode is silent and the blast radius is the production database, which
is why this test asserts on the *import list* rather than on a schema. Comparing
schemas cannot catch a module that never reached the comparison.

`alembic/env.py` and `app/models/__init__.py` must therefore agree, and the set of
modules they name must equal the set of modules that actually exist on disk. All
three are checked: two files that drift from each other produce exactly bug 1.
"""

import ast
from pathlib import Path

import pytest

import app.models

MODELS_DIR = Path(app.models.__file__).parent
REPO_ROOT = Path(app.models.__file__).resolve().parents[2]

#: Modules under `app/models/` that are not models. `base` holds the mixins; it is
#: imported by `alembic/env.py` for the side effect of registering nothing, and
#: deliberately not re-exported from `app.models`, so it is excluded from every
#: comparison below rather than being papered over.
NON_MODEL_MODULES = {"base"}


def _env_imported_modules() -> set[str]:
    """Model modules `alembic/env.py` imports for their side effects."""
    tree = ast.parse((REPO_ROOT / "alembic" / "env.py").read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "app.models":
            # `from app.models import ad as _ad` — the side-effect import form.
            imported.update(alias.name for alias in node.names)
    return imported


def _init_exported_modules() -> set[str]:
    """Model modules `app/models/__init__.py` imports."""
    tree = ast.parse((MODELS_DIR / "__init__.py").read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(
            "app.models."
        ):
            imported.add(node.module.removeprefix("app.models."))
    return imported


def _modules_on_disk() -> set[str]:
    """Every model module that exists, minus the ones that are not models."""
    return {
        path.stem
        for path in MODELS_DIR.glob("*.py")
        if path.stem not in NON_MODEL_MODULES | {"__init__"}
    }


@pytest.mark.parametrize(
    ("listed", "where"),
    [
        pytest.param(_env_imported_modules(), "alembic/env.py", id="env"),
        pytest.param(_init_exported_modules(), "app/models/__init__.py", id="init"),
    ],
)
def test_every_model_module_is_listed(listed: set[str], where: str) -> None:
    missing = _modules_on_disk() - listed
    assert not missing, (
        f"{where} does not import {sorted(missing)}. A model module that is not "
        "imported is a table autogenerate cannot see and `alembic check` cannot "
        "compare — add the import to both alembic/env.py and app/models/__init__.py."
    )


def test_env_and_init_list_the_same_modules() -> None:
    """Two lists that drift apart are how `conversions` got dropped."""
    in_env = _env_imported_modules() - NON_MODEL_MODULES
    in_init = _init_exported_modules() - NON_MODEL_MODULES
    assert in_env == in_init, (
        "alembic/env.py and app/models/__init__.py list different model modules. "
        f"env-only: {sorted(in_env - in_init)}; "
        f"init-only: {sorted(in_init - in_env)}"
    )


def test_every_exported_name_resolves() -> None:
    """`__all__` is an import list, not a wish list.

    A name in `__all__` with no binding behind it fails only at the point some
    other module does `from app.models import X` — which for a table model means
    a query helper nobody wrote yet.
    """
    missing = [name for name in app.models.__all__ if not hasattr(app.models, name)]
    assert not missing, f"app.models.__all__ names {missing}, which do not exist"


def test_every_table_has_a_primary_key() -> None:
    """Autogenerate needs a PK to diff a table; a model without one is invisible.

    Cheap to assert, and the failure it prevents is a migration that cannot be
    reversed.
    """
    from sqlalchemy import inspect

    from app.core.database import Base

    missing = [
        table.name for table in Base.metadata.sorted_tables if not inspect(table).primary_key
    ]
    assert not missing, f"tables without a primary key: {missing}"
