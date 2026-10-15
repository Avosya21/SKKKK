from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class NewProjectPerson(BaseModel):
    """Человек заказчика при создании проекта: в портал он может не входить."""

    name: str = Field(min_length=1, max_length=200)
    job_title: str | None = Field(default=None, max_length=200)
    can_approve: bool = False


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    # Стороны и люди (PLATFORM-FOUNDATION-PLAN, ПФ4): всё необязательно, дополнить можно позже в карточке «Люди и стороны»
    customer_company: str | None = Field(default=None, max_length=200)
    executor_company: str | None = Field(default=None, max_length=200)
    customer_people: list[NewProjectPerson] = Field(default_factory=list, max_length=30)


class ProjectOut(BaseModel):
    id: int
    name: str
    description: str | None
    created_by: int
    created_at: datetime
    is_active: bool
    # Кто завёл проект («Фамилия Имя» из карточки, иначе логин) — только в списке для админа.
    creator_name: str | None = None

    model_config = {"from_attributes": True}
