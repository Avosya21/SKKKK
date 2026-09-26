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


class LoginStepResponse(BaseModel):
    """Пароль роли принят, нужен шаг 2: `setup` — придумать личный пароль, `personal` — ввести его."""

    step: str
    ticket: str
    name: str
    can_skip: bool = False
    min_length: int


class TicketRequest(BaseModel):
    ticket: str = Field(min_length=1, max_length=2048)


class PersonalLoginRequest(TicketRequest):
    password: str = Field(min_length=1, max_length=1024)


class PersonalSetupRequest(TicketRequest):
    password: str = Field(max_length=1024)
    password_confirm: str = Field(max_length=1024)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(default="", max_length=1024)
    password: str = Field(max_length=1024)
    password_confirm: str = Field(max_length=1024)


class PasswordStateResponse(BaseModel):
    mode: str
    has_password: bool
    min_length: int
