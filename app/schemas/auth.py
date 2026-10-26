from pydantic import BaseModel, EmailStr, Field, field_validator


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class GoogleLoginRequest(BaseModel):
    id_token: str


class EmailOtpRequest(BaseModel):
    """Ask for a sign-in code. The response is deliberately identical for every
    address, so the schema carries nothing that would reveal account state."""

    email: EmailStr

    @field_validator("email")
    @classmethod
    def _normalize(cls, value: str) -> str:
        return value.strip().lower()


class EmailOtpVerifyRequest(BaseModel):
    email: EmailStr
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("code")
    @classmethod
    def _strip_code(cls, value: str) -> str:
        return value.strip()


class UnsubscribeRequest(BaseModel):
    """A signed token from the unsubscribe link in an alert email."""

    token: str = Field(min_length=1, max_length=1024)
