from fastapi import FastAPI
from sqladmin import Admin
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.admin.auth import AdminAuth, RoleAuthorization
from app.admin.views import ALL_VIEWS


def setup_admin(
    app: FastAPI,
    engine: AsyncEngine,
    session_maker: async_sessionmaker[AsyncSession],
    *,
    secret_key: str,
    https_only: bool = False,
) -> Admin:
    """Mount the back office at /admin."""
    admin = Admin(
        app,
        engine,
        session_maker=session_maker,
        base_url="/admin",
        title="BachatBase Admin",
        authentication_backend=AdminAuth(secret_key, session_maker, https_only=https_only),
        authorization_backend=RoleAuthorization(),
    )
    for view in ALL_VIEWS:
        admin.add_view(view)
    return admin
