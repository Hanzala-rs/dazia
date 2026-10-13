"""Shared result type for all DAZIA tools."""
from pydantic import BaseModel, Field


class ToolResult(BaseModel):
    ok: bool
    message: str  # short, user-facing text
    needs_clarification: bool = False
    data: dict = Field(default_factory=dict)
