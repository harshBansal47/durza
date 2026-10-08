"""Authentication and authorization for the /admin back office.

Only active users with the admin or editor role may sign in. The session cookie
stores just the user id; every request re-reads the user from the database, so
deactivating or demoting someone takes effect immediately.
"""

import uuid
from typing import Any

from sqladmin.authentication import AuthenticationBackend
from sqladmin.authorization import Action, AuthorizationBackend
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request

from app.core.security import hash_password, verify_password
from app.models.user import Role, User
from app.services import user_service

STAFF_ROLES = frozenset({Role.admin, Role.editor})
SESSION_MAX_AGE = 8 * 60 * 60  # an 8-hour working day

# Verified when the email is unknown, so "no such user" and "wrong password"
# take the same time and can't be told apart by timing.
_DUMMY_HASH = hash_password("not-a-real-password")


class AdminAuth(AuthenticationBackend):
    def __init__(
        self,
        secret_key: str,
        session_maker: async_sessionmaker[AsyncSession],
        *,
        https_only: bool = False,
    ) -> None:
        super().__init__(
            secret_key=secret_key,
            https_only=https_only,
            same_site="lax",
            max_age=SESSION_MAX_AGE,
        )
        self._session_maker = session_maker

    async def login(self, request: Request) -> bool:
        form = await request.form()
        email = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))

        async with self._session_maker() as db:
            user = await user_service.get_user_by_email(db, email)

        if user is None:
            verify_password(password, _DUMMY_HASH)
            return False
        if not verify_password(password, user.hashed_password):
            return False
        if not user.is_active or user.role not in STAFF_ROLES:
            return False

        request.session.clear()
        request.session["user_id"] = str(user.id)
        return True

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        raw_id = request.session.get("user_id")
        if not raw_id:
            return False
        try:
            user_id = uuid.UUID(raw_id)
        except ValueError:
            return False

        async with self._session_maker() as db:
            user = await db.get(User, user_id)

        if user is None or not user.is_active or user.role not in STAFF_ROLES:
            request.session.clear()
            return False

        request.state.admin_role = user.role
        return True


class RoleAuthorization(AuthorizationBackend):
    """Admins can do everything; editors manage content but cannot delete it or touch users."""

    def has_permission(
        self, request: Request, identity: str, action: str, obj: Any | None = None
    ) -> bool:
        role = getattr(request.state, "admin_role", None)
        if role == Role.admin:
            return True
        if role == Role.editor:
            return identity != "user" and action != Action.DELETE
        return False
