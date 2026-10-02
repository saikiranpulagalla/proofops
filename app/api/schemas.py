from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class CreateRunRequest(BaseModel):
    goal_text: str = Field(min_length=1, max_length=2000)
    company: str = Field(min_length=1, max_length=255)
    deadline: datetime | None = None

    @field_validator("goal_text", "company")
    @classmethod
    def no_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class ApproveRequest(BaseModel):
    action_id: int = Field(gt=0)
    ttl_seconds: int = Field(default=600, ge=30, le=3600)


class ExecuteRequest(BaseModel):
    action_id: int = Field(gt=0)
    approval_id: int = Field(gt=0)


class RejectRequest(BaseModel):
    action_id: int = Field(gt=0)
    approval_id: int = Field(gt=0)


class CheckpointResponse(BaseModel):
    run_id: int
    run_state: str
    action_id: int | None = None
    action_state: str | None = None
    approval_id: int | None = None
    target: str | None = None
    subject: str | None = None
    body: str | None = None
    reasons: list[str] = Field(default_factory=list)


class ApiError(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None
