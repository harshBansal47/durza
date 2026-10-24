import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.user import Role


class UserCreate(BaseModel):
    """Internal/admin user creation.

    Public sign-up no longer takes a password: it happens through Google or an
    email code. Staff accounts are still created here with one, because the
    back office has its own password login.
    """

    email: EmailStr
    password: str | None = Field(default=None, min_length=8, max_length=128)
    full_name: str | None = None


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    full_name: str | None
    # Google photo URL when the account has signed in with Google.
    avatar_url: str | None = None
    role: Role
    is_active: bool
    created_at: datetime


class UserUpdate(BaseModel):
    """Self-service profile edits.

    Email is deliberately absent: changing an address needs re-verification, and
    half-implementing it would let someone lock themselves out of their alerts.
    Passwords are absent too — public accounts do not have one.
    """

    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, max_length=255)


class DeleteAccountRequest(BaseModel):
    """Confirmation for a self-service account deletion.

    The signed-in session is the authority; typing the account's own address is
    a deliberate speed bump against a stray click, not a second credential.
    """

    confirm_email: EmailStr

    @field_validator("confirm_email")
    @classmethod
    def _normalize(cls, value: str) -> str:
        return value.strip().lower()
