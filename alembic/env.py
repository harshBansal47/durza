import asyncio
import sys
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from app.core.config import get_settings
from app.core.database import Base
from app.core.migration_guard import enforce_from_environment, resolve_command

# Import every model module here so its table is registered on Base.metadata
# before autogenerate compares it against the live database.
#
# This list is explicit and complete on purpose, and there is a test
# (`tests/test_models_registered.py`) that fails if it ever drifts from
# `app.models.__all__`. Two real bugs came from an omission here: `conversions`
# was invisible to autogenerate, so a migration was generated that silently
# DROPPED it, and `alembic check` reported the database as clean while the
# drift was present. An unlisted module is a table nobody is checking.
from app.models import ad as _ad  # noqa: F401
from app.models import affiliate_destination as _affiliate_destination  # noqa: F401
from app.models import affiliate_program as _affiliate_program  # noqa: F401
from app.models import affiliate_provider as _affiliate_provider  # noqa: F401
from app.models import affiliate_revenue as _affiliate_revenue  # noqa: F401
from app.models import analytics_event as _analytics_event  # noqa: F401
from app.models import analytics_session as _analytics_session  # noqa: F401
from app.models import app_log as _app_log  # noqa: F401
from app.models import attribution as _attribution  # noqa: F401
from app.models import base as _base  # noqa: F401
from app.models import category as _category  # noqa: F401
from app.models import click as _click  # noqa: F401
from app.models import conversion as _conversion  # noqa: F401
from app.models import coupon as _coupon  # noqa: F401
from app.models import coupon_impression as _coupon_impression  # noqa: F401
from app.models import coupon_source as _coupon_source  # noqa: F401
from app.models import coupon_staging as _coupon_staging  # noqa: F401
from app.models import coupon_verification as _coupon_verification  # noqa: F401
from app.models import daily_stats as _daily_stats  # noqa: F401
from app.models import editorial as _editorial  # noqa: F401
from app.models import ingestion_run as _ingestion_run  # noqa: F401
from app.models import job as _job  # noqa: F401
from app.models import page as _page  # noqa: F401
from app.models import product as _product  # noqa: F401
from app.models import revenue_event as _revenue_event  # noqa: F401
from app.models import sale as _sale  # noqa: F401
from app.models import source as _source  # noqa: F401
from app.models import sponsored as _sponsored  # noqa: F401
from app.models import store as _store  # noqa: F401
from app.models import tracking as _tracking  # noqa: F401
from app.models import user as _user  # noqa: F401
from app.models import user_interest as _user_interest  # noqa: F401
from app.models import user_segment as _user_segment  # noqa: F401
from app.models import verification_attempt as _verification_attempt  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# `compare_type=True` is what makes a Postgres ENUM addition visible to
# autogenerate and to `alembic check`. Without it, adding `awin` to
# `SourceKind` in Python changes nothing about the database, the migration
# generated for it has an empty `upgrade()`, and the first attempt to insert a
# source row fails on an invalid enum value. `compare_server_default` catches
# the same class of problem for columns added without a server default.
COMPARE_OPTIONS: dict[str, bool] = {
    "compare_type": True,
    "compare_server_default": True,
}

settings = get_settings()


# Which alembic command is running, and whether it may run against production.
#
# The check happens here, before the engine is created and before any
# connection is opened, so a refused command touches nothing at all.
#
# `downgrade` and `stamp` are refused in production with no override.
# `upgrade` needs ALLOW_PROD_MIGRATION=1 in the environment of that one run.
# Everything else is read-only and allowed.
def _running_command() -> str:
    """The alembic command this process is running.

    Read from the command line, which is how alembic is invoked everywhere
    here (`alembic upgrade head` in the deploy step, `alembic check` in CI).
    A command that cannot be identified stays unidentified, and an
    unidentified command is refused in production rather than guessed at.
    """
    return resolve_command(sys.argv)


enforce_from_environment(_running_command(), settings.environment)
# ConfigParser would try to interpolate "%" in e.g. URL-encoded passwords.
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **COMPARE_OPTIONS,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, **COMPARE_OPTIONS)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = create_async_engine(settings.database_url, poolclass=pool.NullPool)

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
