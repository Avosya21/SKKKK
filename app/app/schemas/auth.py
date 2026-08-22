from __future__ import annotations

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    telegram_id: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    name: str


class UserOut(BaseModel):
    id: int
    name: str
    telegram_id: str
    role: str
    position: str | None = None

    model_config = {"from_attributes": True}
