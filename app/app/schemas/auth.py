from __future__ import annotations

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    # Необязателен: поле на форме входа скрыто (решение владельца 2026-09-28) — уведомлений в Telegram нет
    telegram_id: str | None = Field(default=None, max_length=64)
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
    """Нужен следующий шаг входа: `setup` — придумать личный пароль, `personal` — ввести его,
    `profile` — заполнить карточку."""

    step: str
    ticket: str
    name: str
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


class ProfileCompanyOut(BaseModel):
    id: int
    name: str


class ProfileOptionsResponse(BaseModel):
    """Что нужно форме карточки при входе: компании (без стороны) и уведомление о согласии."""

    name: str
    companies: list[ProfileCompanyOut]
    company_locked: bool
    company_id: int | None
    consent_text: str
    consent_details: str
    consent_version: str
    max_photo_mb: int
