"""
WebSocket message models for validation and type safety.
"""
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

MIN_TERMINAL_DIMENSION = 1
MAX_TERMINAL_DIMENSION = 500


class ResizePayload(BaseModel):
    """Terminal resize command."""

    rows: int = Field(default=24, ge=MIN_TERMINAL_DIMENSION, le=MAX_TERMINAL_DIMENSION)
    cols: int = Field(default=80, ge=MIN_TERMINAL_DIMENSION, le=MAX_TERMINAL_DIMENSION)


class ProxyCommand(BaseModel):
    """AI Proxy control command."""

    enable: Optional[bool] = None
    disable: Optional[bool] = None
    provider: Optional[str] = None
    system_prompt: Optional[str] = None


class ClaudeCodeCommand(BaseModel):
    """Claude Code auto-continue control command."""

    enable: Optional[bool] = None
    disable: Optional[bool] = None
    screen_text: Optional[str] = None


class BrowserAgentSubmitPayload(BaseModel):
    """Browser ask-mode submission payload."""

    prompt: str
    provider: Optional[str] = None


class BrowserAgentApprovePayload(BaseModel):
    """Browser ask-mode approval payload."""

    target: str
    scope: str
    command_ids: Optional[list[str | int]] = None
    pattern: Optional[str] = None


class BrowserAgentCommand(BaseModel):
    """Browser ask-mode control payload."""

    submit: Optional[BrowserAgentSubmitPayload] = None
    approve: Optional[BrowserAgentApprovePayload] = None
    stop: Optional[bool] = None


class WebSocketMessage(BaseModel):
    """WebSocket message from client."""

    model_config = ConfigDict(extra="allow")

    input: Optional[str] = None
    resize: Optional[ResizePayload] = None
    proxy: Optional[ProxyCommand] = None
    claude_code: Optional[ClaudeCodeCommand] = None
    browser_agent: Optional[BrowserAgentCommand] = None
