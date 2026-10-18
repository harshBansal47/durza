from pydantic import BaseModel


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class GoogleLoginRequest(BaseModel):
    id_token: str
