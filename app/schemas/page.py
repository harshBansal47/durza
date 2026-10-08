import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class PageCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    meta_description: str | None = Field(default=None, max_length=300)
    is_published: bool = True


class PageUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    content: str | None = Field(default=None, min_length=1)
    meta_description: str | None = Field(default=None, max_length=300)
    is_published: bool | None = None


class PageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    slug: str
    content: str
    meta_description: str | None
    is_published: bool
    created_at: datetime
