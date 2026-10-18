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
    role: Role
    is_active: bool
    has_password: bool = False
    created_at: datetime

    @model_validator(mode="before")
    @classmethod
    def _set_has_password(cls, data):
        # SQLAlchemy User object: derive has_password from hashed_password.
        if hasattr(data, "hashed_password"):
            data_dict = {k: getattr(data, k) for k in ("id","email","full_name","role","is_active","created_at")}
            data_dict["has_password"] = data.hashed_password is not None
            return data_dict
        return data


class UserUpdate(BaseModel):
    """Self-service profile edits.

    Email is deliberately absent: changing an address needs re-verification, and
    half-implementing it would let someone lock themselves out of their alerts.
    """

    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, min_length=8, max_length=128)
    current_password: str | None = None


class SetPasswordRequest(BaseModel):
    new_password: str = Field(min_length=8, max_length=128)
    """Request a password reset email."""

    email: EmailStr


class ResetPasswordRequest(BaseModel):
    """Reset password using a token from the reset email."""

    token: str
    new_password: str = Field(min_length=8, max_length=128)
