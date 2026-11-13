from typing import Literal

from pydantic import BaseModel, Field


class RunRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=160)
    confidence_threshold: float = Field(default=0.85, ge=0, le=1)
    variance_threshold: float = Field(default=0.50, ge=0.05, le=5)


class ReviewRequest(BaseModel):
    decision: Literal["approve", "correct", "reject"]
    corrected_value: float | None = None
    reviewer: str = Field(default="reviewer", min_length=1, max_length=100)
    note: str = Field(default="", max_length=1000)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=2000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    run_id: str | None = None
    history: list[ChatMessage] = Field(default_factory=list, max_length=12)
