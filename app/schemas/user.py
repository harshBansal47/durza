import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.models.user import Role


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
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
    # Never the hash itself — just whether one exists. Google-only accounts
    # have none, and the settings page needs to offer "set a password" instead
    # of "change password" for exactly those.
    has_password: bool = False
    created_at: datetime

    @model_validator(mode="before")
    @classmethod
    def _derive_has_password(cls, data: object) -> object:
        """Turn `hashed_password is not None` into the `has_password` field.

        The validator runs before attribute extraction, so an ORM `User` is
        flattened into a dict with the derived flag. Anything that is already a
        dict (constructing a schema by hand) passes through untouched.
        """
        if not hasattr(data, "hashed_password"):
            return data
        data_dict = {
            name: getattr(data, name) for name in cls.model_fields if name != "has_password" and hasattr(data, name)
        }
        data_dict["has_password"] = getattr(data, "hashed_password") is not None
        return data_dict


class UserUpdate(BaseModel):
    """Self-service profile edits.

    Email is deliberately absent: changing an address needs re-verification, and
    half-implementing it would let someone lock themselves out of their alerts.
    """

    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, min_length=8, max_length=128)
    current_password: str | None = None


class ForgotPasswordRequest(BaseModel):
    """Request a password reset email."""

    email: EmailStr


class SetPasswordRequest(BaseModel):
    """First password for an account that has none (e.g. created via Google)."""

    new_password: str = Field(min_length=8, max_length=128)


class ResetPasswordRequest(BaseModel):
    """Reset password using a token from the reset email."""

    token: str
    new_password: str = Field(min_length=8, max_length=128)
