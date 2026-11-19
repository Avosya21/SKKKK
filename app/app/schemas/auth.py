from __future__ import annotations

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    # Необязателен: поле на форме входа скрыто (решение владельца 2026-09-28).
    telegram_id: str | None = Field(default=None, max_length=64)
    # Личный пароль (при PERSONAL_PASSWORD=required обязателен для входа).
    password: str | None = Field(default=None, max_length=1024)
    # Выбранная роль; при одной доступной можно не передавать (обычно на шаге кода).
    role: str | None = Field(default=None, max_length=32)


class RegisterRequest(BaseModel):
    """Первый вход: только логин из allowlist, без пароля до кода."""

    name: str = Field(min_length=1, max_length=255)


class RoleOption(BaseModel):
    role: str
    label: str


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
    """Следующий шаг: ``code`` | ``setup`` | ``profile`` (и устар. personal — не используется)."""

    step: str
    ticket: str
    name: str
    min_length: int = 0
    role: str | None = None
    roles: list[RoleOption] = Field(default_factory=list)
    channel: str | None = None
    channels: list[str] = Field(default_factory=list)
    code_hint: str | None = None


class LoginProbeResponse(BaseModel):
    """Подсказка UI до отправки формы: какие роли и нужен ли личный пароль."""

    name: str
    roles: list[RoleOption]
    needs_personal_password: bool
    is_customer: bool


class TicketRequest(BaseModel):
    ticket: str = Field(min_length=1, max_length=2048)


class CodeRequestBody(TicketRequest):
    channel: str = Field(min_length=1, max_length=16)  # b24 | email


class CodeVerifyBody(TicketRequest):
    code: str = Field(min_length=1, max_length=32)
    # При нескольких ролях у логина — обязателен выбор на шаге кода.
    role: str | None = Field(default=None, max_length=32)


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
