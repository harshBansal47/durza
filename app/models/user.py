import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPKMixin


class Role(str, enum.Enum):
    admin = "admin"
    editor = "editor"
    user = "user"
    analytics_viewer = "analytics_viewer"


class User(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Google account photo URL, refreshed on every Google sign-in. Password
    # accounts have none; the UI falls back to initials when it is missing.
    avatar_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Set when the address is proven: Google says so on every ID token, and a
    # consumed email code proves control of the inbox. Password-only staff
    # accounts may legitimately have NULL here.
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Included in every JWT as `ver` and compared on each request. Logging out
    # increments it, which revokes every token issued before that moment.
    token_version: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    role: Mapped[Role] = mapped_column(
        Enum(Role, name="user_role", native_enum=True), default=Role.user, nullable=False
    )
    personalization_consent: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    def __str__(self) -> str:
        return self.email


class EmailOtpChallenge(Base, UUIDPKMixin, TimestampMixin):
    """A passwordless sign-in code, stored as an HMAC digest.

    The row is deliberately not tied to a user id. A request for an address
    with no account must look and behave exactly like one for an existing
    account — including creating a challenge and sending mail — or the endpoint
    becomes an account-enumeration oracle. The account is resolved (and created,
    under the product's open-registration policy) only after the code verifies.

    One active challenge per address: issuing a new code consumes every older
    unconsumed row for that address, so a code is single-use even if the user
    requests several and keeps an old email open.
    """

    __tablename__ = "email_otp_challenges"

    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Hashed with the same salt as every other IP-derived value in the app. It
    # is useful for abuse investigation and should not outlive the challenge.
    request_ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    def __str__(self) -> str:
        return f"email code for {self.email}"
