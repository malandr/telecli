"""
Session manager for coordinating multiple terminal sessions.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.ai_proxy import AIProxy
from src.browser_agent import ASK_MODE_SYSTEM_PROMPT, BrowserAgentController
from src.claude_code_auto_continue import ClaudeCodeAutoContinue
from src.config import Config
from src.llm_provider import LLMProviderFactory
from src.llm_providers import *  # Register all providers
from src.terminal import (
    DEFAULT_TERMINAL_COLS,
    DEFAULT_TERMINAL_ROWS,
    TerminalSession,
    TmuxSession,
)
from src.tmux import (
    capture_tmux_pane,
    capture_tmux_screen,
    create_tmux_session,
    get_tmux_interaction_recommendation,
    get_tmux_pane_state,
    list_tmux_sessions,
    send_tmux_key,
    tmux_session_exists,
)

logger = logging.getLogger(__name__)
_TMUX_AVAILABILITY_CACHE_TTL_SECONDS = 1.0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SessionRecord:
    """Session metadata exposed to the UI."""

    session_id: str
    name: str
    backend: str = "telecli"
    created_at: str = field(default_factory=_utc_now_iso)
    tmux_session_name: Optional[str] = None


class SessionManager:
    """Manages multiple terminal sessions."""

    def __init__(
        self,
        max_sessions: int = Config.TERMINAL_MAX_SESSIONS,
        registry_path: Optional[Path] = None,
        user_id: Optional[str] = None,
    ):
        self.max_sessions = max_sessions
        self.registry_path = Path(registry_path) if registry_path else Path(Config.SESSION_REGISTRY_PATH)
        self.user_id = user_id
        self.default_session_id = user_id or "default"
        self.sessions: dict[str, TerminalSession | TmuxSession] = {}
        self.session_records: dict[str, SessionRecord] = {}
        self.session_count = 0
        self.ai_proxies: dict[str, AIProxy] = {}
        self.browser_agents: dict[str, BrowserAgentController] = {}
        self.claude_code_auto_controllers: dict[str, ClaudeCodeAutoContinue] = {}
        self.monitor_callback = None
        self._tmux_availability_cache: dict[str, tuple[float, bool]] = {}
        self._load_persisted_tmux_records()

    @property
    def ai_proxy(self) -> Optional[AIProxy]:
        """Backward-compatible accessor for the default session AI proxy."""
        return self.ai_proxies.get(self.default_session_id)

    def _load_persisted_tmux_records(self) -> None:
        if not self.registry_path.exists():
            return

        try:
            payload = json.loads(self.registry_path.read_text())
        except Exception as e:
            logger.error("Failed to load session registry %s: %s", self.registry_path, e)
            return

        for item in payload.get("sessions", []):
            if item.get("backend") != "tmux":
                continue

            record = SessionRecord(
                session_id=item["session_id"],
                name=item.get("name") or item["session_id"],
                backend="tmux",
                created_at=item.get("created_at") or _utc_now_iso(),
                tmux_session_name=item.get("tmux_session_name"),
            )
            self.session_records[record.session_id] = record

    def _save_persisted_tmux_records(self) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "sessions": [
                asdict(record)
                for record in self.session_records.values()
                if record.backend == "tmux"
            ]
        }
        self.registry_path.write_text(json.dumps(payload, indent=2, sort_keys=True))

    @staticmethod
    def _generate_session_id(prefix: str) -> str:
        return f"{prefix}-{secrets.token_hex(16)}"

    def _ensure_record(
        self,
        session_id: str,
        *,
        backend: str = "telecli",
        name: Optional[str] = None,
        tmux_session_name: Optional[str] = None,
    ) -> SessionRecord:
        record = self.session_records.get(session_id)
        if record:
            return record

        record = SessionRecord(
            session_id=session_id,
            name=name or session_id,
            backend=backend,
            tmux_session_name=tmux_session_name,
        )
        self.session_records[session_id] = record
        if backend == "tmux":
            self._save_persisted_tmux_records()
        return record

    def _resolve_record(self, session_id: str) -> SessionRecord:
        record = self.session_records.get(session_id)
        if record:
            return record

        active_session = self.sessions.get(session_id)
        if isinstance(active_session, TmuxSession):
            return self._ensure_record(
                session_id,
                backend="tmux",
                tmux_session_name=active_session.tmux_session_name,
            )

        return self._ensure_record(session_id, backend="telecli")

    def _prune_runtime_session(self, session_id: str) -> None:
        runtime = self.sessions.get(session_id)
        if runtime and not runtime.is_active:
            self.sessions.pop(session_id, None)

    def _session_available(self, record: SessionRecord) -> bool:
        runtime = self.sessions.get(record.session_id)
        if runtime and runtime.is_active:
            return True
        if record.backend == "tmux" and record.tmux_session_name:
            return self._tmux_session_available(record.tmux_session_name)
        return False

    def _tmux_session_available(self, session_name: str) -> bool:
        cached = self._tmux_availability_cache.get(session_name)
        now = time.monotonic()
        if cached and now - cached[0] < _TMUX_AVAILABILITY_CACHE_TTL_SECONDS:
            return cached[1]

        available = tmux_session_exists(session_name)
        self._tmux_availability_cache[session_name] = (now, available)
        return available

    def get_session_summary(self, session_id: str) -> dict:
        record = self._resolve_record(session_id)
        runtime = self.sessions.get(session_id)
        is_active = bool(runtime and runtime.is_active)
        available = self._session_available(record)

        if runtime:
            shell = runtime.shell
        elif record.backend == "tmux" and record.tmux_session_name:
            shell = f"tmux:{record.tmux_session_name}"
        else:
            shell = Config.TERMINAL_SHELL

        return {
            "id": record.session_id,
            "name": record.name,
            "backend": record.backend,
            "created_at": record.created_at,
            "shell": shell,
            "is_active": is_active,
            "available": available,
            "tmux_session_name": record.tmux_session_name,
        }

    def get_session_mode_capabilities(self, session_id: str) -> dict:
        """Describe whether the session supports Telegram agent mode."""
        record = self._resolve_record(session_id)
        tmux_available = bool(
            record.backend == "tmux"
            and record.tmux_session_name
            and self._tmux_session_available(record.tmux_session_name)
        )
        return {
            "backend": record.backend,
            "supports_agent_mode": tmux_available,
            "tmux_session_name": record.tmux_session_name,
        }

    def get_agent_mode_recommendation(self, session_id: str) -> dict:
        """Return a recommendation summary for Telegram agent mode."""
        record = self._resolve_record(session_id)
        capabilities = self.get_session_mode_capabilities(session_id)
        if not capabilities["supports_agent_mode"]:
            return {
                "supports_agent_mode": False,
                "should_suggest_agent_mode": False,
                "reason": "Session is not tmux-backed or backing tmux session is unavailable",
                "signature": None,
            }

        return get_tmux_interaction_recommendation(record.tmux_session_name)

    def list_sessions(self) -> list[dict]:
        for session_id in list(self.sessions):
            self._prune_runtime_session(session_id)

        summaries = []
        seen_ids: set[str] = set()

        for session_id, session in self.sessions.items():
            if not session.is_active:
                continue
            seen_ids.add(session_id)
            summaries.append(self.get_session_summary(session_id))

        for record in self.session_records.values():
            if record.session_id in seen_ids:
                continue
            summaries.append(self.get_session_summary(record.session_id))

        return sorted(
            summaries,
            key=lambda session: (
                session["backend"] != "tmux",
                session["name"].lower(),
                session["id"],
            ),
        )

    def list_machine_tmux_sessions(self) -> list[dict]:
        imported_by_tmux_name = {
            record.tmux_session_name: record
            for record in self.session_records.values()
            if record.backend == "tmux" and record.tmux_session_name
        }

        machine_sessions = []
        for session in list_tmux_sessions():
            imported_record = imported_by_tmux_name.get(session["name"])
            try:
                pane = get_tmux_pane_state(session["name"])
            except ValueError as exc:
                logger.debug("Failed to inspect tmux pane state for %s: %s", session["name"], exc)
                pane = {}
            machine_sessions.append(
                {
                    "name": session["name"],
                    "windows": session["windows"],
                    "attached": session["attached"],
                    "imported": imported_record is not None,
                    "imported_session_id": imported_record.session_id if imported_record else None,
                    "imported_name": imported_record.name if imported_record else None,
                    "pane_id": pane.get("pane_id"),
                    "current_command": pane.get("current_command"),
                    "current_path": pane.get("current_path"),
                    "pane_paths": pane.get("pane_paths", []),
                    "alternate_screen": pane.get("alternate_screen", False),
                    "interactive": pane.get("interactive", False),
                }
            )

        return machine_sessions

    def create_session_entry(self, name: Optional[str] = None) -> dict:
        session_id = self._generate_session_id("web")
        self._ensure_record(session_id, backend="telecli", name=(name or session_id).strip())
        return self.get_session_summary(session_id)

    def rename_session(self, session_id: str, name: str) -> dict:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Session name cannot be empty")

        record = self._resolve_record(session_id)
        record.name = clean_name
        if record.backend == "tmux":
            self._save_persisted_tmux_records()
        return self.get_session_summary(session_id)

    def import_tmux_session(self, tmux_session_name: str, name: Optional[str] = None) -> dict:
        clean_tmux_name = tmux_session_name.strip()
        if not clean_tmux_name:
            raise ValueError("tmux session name cannot be empty")

        for record in self.session_records.values():
            if record.backend == "tmux" and record.tmux_session_name == clean_tmux_name:
                return self.get_session_summary(record.session_id)

        if not any(session["name"] == clean_tmux_name for session in self.list_machine_tmux_sessions()):
            raise KeyError(f"tmux session {clean_tmux_name} does not exist")

        session_id = self._generate_session_id("tmux")
        self._ensure_record(
            session_id,
            backend="tmux",
            name=(name or clean_tmux_name).strip(),
            tmux_session_name=clean_tmux_name,
        )
        return self.get_session_summary(session_id)

    def create_tmux_session_entry(self, tmux_session_name: str, name: Optional[str] = None) -> dict:
        clean_tmux_name = tmux_session_name.strip()
        if not clean_tmux_name:
            raise ValueError("tmux session name cannot be empty")

        if any(
            record.backend == "tmux" and record.tmux_session_name == clean_tmux_name
            for record in self.session_records.values()
        ):
            raise ValueError(f"tmux session already imported: {clean_tmux_name}")

        create_tmux_session(clean_tmux_name)
        session_id = self._generate_session_id("tmux")
        self._ensure_record(
            session_id,
            backend="tmux",
            name=(name or clean_tmux_name).strip(),
            tmux_session_name=clean_tmux_name,
        )
        return self.get_session_summary(session_id)

    async def detach_tmux_session(self, session_id: str) -> dict:
        record = self.session_records.get(session_id)
        if not record:
            raise KeyError("Session not found")
        if record.backend != "tmux":
            raise ValueError("Only tmux sessions can be detached")

        await self.close_session(session_id)
        return self.get_session_summary(session_id)

    async def delete_session_entry(self, session_id: str) -> None:
        if session_id not in self.session_records and session_id not in self.sessions:
            logger.debug("Session entry %s already removed", session_id)
            return

        await self.close_session(session_id)

        record = self.session_records.pop(session_id, None)
        if record and record.backend == "tmux":
            self._save_persisted_tmux_records()

    async def get_session(
        self,
        session_id: str,
        *,
        rows: int | None = None,
        cols: int | None = None,
    ) -> TerminalSession | TmuxSession:
        """Get or create a session for the given ID."""
        self._prune_runtime_session(session_id)
        if session_id in self.sessions:
            session = self.sessions[session_id]
            if rows is not None or cols is not None:
                await session.resize(
                    rows if rows is not None else getattr(session, "initial_rows", DEFAULT_TERMINAL_ROWS),
                    cols if cols is not None else getattr(session, "initial_cols", DEFAULT_TERMINAL_COLS),
                )
            return session

        if len(self.sessions) >= self.max_sessions:
            logger.warning("Max sessions reached, closing oldest active session")
            oldest_id = next(iter(self.sessions))
            await self.close_session(oldest_id)

        record = self.session_records.get(session_id)
        if not record:
            record = self._ensure_record(session_id, backend="telecli")

        if record.backend == "tmux":
            if not record.tmux_session_name or not tmux_session_exists(record.tmux_session_name):
                raise RuntimeError(f"tmux session not available: {record.tmux_session_name}")
            session = TmuxSession(
                session_id,
                record.tmux_session_name,
                initial_rows=rows if rows is not None else DEFAULT_TERMINAL_ROWS,
                initial_cols=cols if cols is not None else DEFAULT_TERMINAL_COLS,
            )
        else:
            session = TerminalSession(
                session_id,
                initial_rows=rows if rows is not None else DEFAULT_TERMINAL_ROWS,
                initial_cols=cols if cols is not None else DEFAULT_TERMINAL_COLS,
            )

        if not await session.start():
            raise RuntimeError(f"Failed to start session {session_id}")

        self.sessions[session_id] = session
        self.session_count += 1
        logger.info(
            "Created new %s session %s, total active sessions: %s",
            record.backend,
            session_id,
            len(self.sessions),
        )
        return session

    async def send_input(self, session_id: str, text: str, newline: bool = True, from_ai: bool = False) -> None:
        """Send input to a session."""
        session = await self.get_session(session_id)
        logger.debug(
            "SessionManager.send_input called with: text=%r, newline=%s, from_ai=%s",
            text[:100],
            newline,
            from_ai,
        )
        await session.send_input(text, newline)
        logger.debug("Sent input to session %s (from_ai=%s)", session_id, from_ai)

    async def _send_text_like_user(self, session_id: str, text: str) -> None:
        """Send text and submit it the same way a user would."""
        logger.info("Sending automation text to session %s: %r", session_id, text[:100])
        for char in text:
            await self.send_input(session_id, char, newline=False, from_ai=True)
        await self.send_input(session_id, "\r", newline=False, from_ai=True)
        logger.info("Submitted automation text for session %s", session_id)

    async def _submit_automation_line(self, session_id: str, text: str) -> None:
        """Submit a complete automation command as a single terminal line."""
        logger.info("Sending automation line to session %s: %r", session_id, text[:100])
        await self.send_input(session_id, text, newline=True, from_ai=True)
        logger.info("Submitted automation line for session %s", session_id)

    async def resize_session(self, session_id: str, rows: int, cols: int) -> None:
        """Resize a terminal session."""
        if session_id in self.sessions:
            await self.sessions[session_id].resize(rows, cols)
            logger.debug("Resized session %s to %sx%s", session_id, rows, cols)

    async def get_output_stream(
        self,
        session_id: str,
        *,
        rows: int | None = None,
        cols: int | None = None,
    ):
        """Get output stream from a session."""
        session = await self.get_session(session_id, rows=rows, cols=cols)
        async for chunk in session.get_output_stream():
            yield chunk

    def capture_session_snapshot(self, session_id: str, *, lines: int = 80) -> str:
        """Capture a stable snapshot for tmux-backed sessions."""
        record = self._resolve_record(session_id)
        if record.backend != "tmux" or not record.tmux_session_name:
            raise ValueError("Agent mode requires a tmux-backed session")

        return capture_tmux_pane(record.tmux_session_name, lines=lines)

    def capture_session_screen(self, session_id: str) -> str:
        """Capture the currently visible pane for tmux-backed sessions."""
        record = self._resolve_record(session_id)
        if record.backend != "tmux" or not record.tmux_session_name:
            raise ValueError("Agent mode requires a tmux-backed session")

        return capture_tmux_screen(record.tmux_session_name)

    def tail_session_output(self, session_id: str, *, lines: int = 20) -> str:
        """Return the last N lines from a tmux pane snapshot."""
        snapshot = self.capture_session_snapshot(session_id, lines=max(lines, 80))
        return "\n".join(snapshot.splitlines()[-lines:])

    async def send_exact_input(self, session_id: str, text: str) -> None:
        """Send raw text without appending a newline."""
        await self.send_input(session_id, text, newline=False, from_ai=False)

    def send_special_key(self, session_id: str, key_name: str) -> None:
        """Send a normalized key sequence to a tmux-backed session."""
        record = self._resolve_record(session_id)
        if record.backend != "tmux" or not record.tmux_session_name:
            raise ValueError("Agent mode requires a tmux-backed session")

        send_tmux_key(record.tmux_session_name, key_name)

    async def send_special_key_async(self, session_id: str, key_name: str) -> None:
        """Offload blocking tmux key sending for async callers."""
        await asyncio.to_thread(self.send_special_key, session_id, key_name)

    async def execute_session_command(self, session_id: str, command: str, timeout: Optional[float] = None) -> str:
        """Execute a command via the session marker flow and wait for completion."""
        session = await self.get_session(session_id)
        return await session.send_command(command, timeout=timeout or Config.TERMINAL_TIMEOUT)

    async def enable_ai_proxy(
        self,
        session_id: Optional[str] = None,
        provider_name: Optional[str] = None,
        system_prompt: Optional[str] = None,
        primary_provider: Optional[str] = None,
    ) -> bool:
        """Enable AI proxy for a session."""
        session_id = session_id or self.default_session_id
        if session_id not in self.session_records and session_id not in self.sessions:
            self._ensure_record(session_id, backend="telecli")

        provider_name = provider_name or primary_provider or Config.AI_PROXY_PROVIDER
        llm_provider = LLMProviderFactory.create(provider_name)
        if not llm_provider:
            logger.error("Failed to create LLM provider: %s", provider_name)
            return False

        available_providers = LLMProviderFactory.get_available_providers()
        fallback_names = [name for name, _ in available_providers if name != provider_name]
        prompt = system_prompt or Config.AI_PROXY_SYSTEM_PROMPT

        ai_proxy = AIProxy(
            llm_provider=llm_provider,
            system_prompt=prompt,
            max_iterations=Config.AI_PROXY_MAX_ITERATIONS,
            fallback_provider_names=fallback_names,
        )

        async def send_input(text: str):
            logger.info("AI proxy sending text to terminal: %r", text[:100])
            await self._send_text_like_user(session_id, text)

        ai_proxy.set_input_callback(send_input)

        if self.monitor_callback:
            ai_proxy.set_monitor_callback(self.monitor_callback)

        ai_proxy.enable()
        self.ai_proxies[session_id] = ai_proxy
        logger.info(
            "Enabled AI proxy for session %s with provider %s, fallbacks: %s",
            session_id,
            provider_name,
            fallback_names if fallback_names else "none",
        )
        return True

    async def disable_ai_proxy(self, session_id: str):
        """Disable AI proxy for a session."""
        if session_id not in self.ai_proxies:
            logger.debug("AI proxy for session %s already disabled or doesn't exist", session_id)
            return

        try:
            self.ai_proxies[session_id].disable()
        except Exception as e:
            logger.error("Error disabling AI proxy for session %s: %s", session_id, e)
        finally:
            self.ai_proxies.pop(session_id, None)
            logger.info("Disabled AI proxy for session %s", session_id)

    def get_ai_proxy(self, session_id: str) -> Optional[AIProxy]:
        """Get AI proxy for a session."""
        return self.ai_proxies.get(session_id)

    def _get_or_create_browser_agent_controller(
        self,
        session_id: str,
        *,
        provider_name: Optional[str] = None,
        system_prompt: Optional[str] = None,
    ) -> BrowserAgentController:
        if session_id not in self.session_records and session_id not in self.sessions:
            self._ensure_record(session_id, backend="telecli")

        provider_name = provider_name or Config.AI_PROXY_PROVIDER
        llm_provider = LLMProviderFactory.create(provider_name)
        if not llm_provider:
            raise RuntimeError(f"Browser agent provider unavailable: {provider_name}")

        controller = self.browser_agents.get(session_id)
        if controller is None:
            controller = BrowserAgentController(
                llm_provider=llm_provider,
                provider_name=provider_name,
                system_prompt=system_prompt or ASK_MODE_SYSTEM_PROMPT,
                execute_command_callback=lambda command: self.execute_session_command(session_id, command),
                interrupt_command_callback=lambda: self.send_input(session_id, "\x03", newline=False, from_ai=True),
                session_id=session_id,
            )
            self.browser_agents[session_id] = controller
        else:
            controller.llm_provider = llm_provider
            controller.provider_name = provider_name
            if system_prompt:
                controller.system_prompt = system_prompt
        return controller

    async def set_browser_agent_event_callback(self, session_id: str, callback) -> None:
        controller = self.browser_agents.get(session_id)
        if controller:
            controller.set_status_callback(callback)

    async def start_browser_agent_run(
        self,
        session_id: str,
        *,
        prompt: str,
        provider_name: Optional[str] = None,
        system_prompt: Optional[str] = None,
        state_callback=None,
    ) -> dict:
        """Start or replace a browser ask-mode run for a session."""
        controller = self._get_or_create_browser_agent_controller(
            session_id,
            provider_name=provider_name,
            system_prompt=system_prompt,
        )
        controller.set_state_callback(state_callback)
        session = await self.get_session(session_id)
        await controller.submit_request(prompt, session_context=session.get_recent_output())
        return controller.get_status()

    async def approve_browser_agent_run(
        self,
        session_id: str,
        *,
        target: str,
        scope: str,
        command_ids: Optional[list[int | str]] = None,
        pattern: Optional[str] = None,
    ) -> dict:
        controller = self.browser_agents.get(session_id)
        if not controller:
            raise RuntimeError("Browser agent is not active for this session")
        await controller.approve(target=target, scope=scope, command_ids=command_ids, pattern=pattern)
        return controller.get_status()

    async def review_browser_agent_plan(self, session_id: str) -> bool:
        await self.approve_browser_agent_run(
            session_id,
            target="review",
            scope="once",
            command_ids=None,
            pattern=None,
        )
        return True

    async def approve_browser_agent_plan(self, session_id: str, scope: str) -> bool:
        await self.approve_browser_agent_run(
            session_id,
            target="plan",
            scope=scope,
            command_ids=None,
            pattern=None,
        )
        return True

    async def approve_browser_agent_command(
        self,
        session_id: str,
        command_id: str,
        scope: str,
        *,
        pattern: Optional[str] = None,
    ) -> bool:
        await self.approve_browser_agent_run(
            session_id,
            target="command",
            scope=scope,
            command_ids=[command_id],
            pattern=pattern,
        )
        return True

    async def stop_browser_agent_run(self, session_id: str) -> dict:
        controller = self.browser_agents.get(session_id)
        if not controller:
            return self.get_browser_agent_status(session_id)
        await controller.stop()
        return controller.get_status()

    async def clear_browser_agent(self, session_id: str) -> None:
        controller = self.browser_agents.pop(session_id, None)
        if controller:
            controller.set_status_callback(None)
            await controller.stop()

    def get_browser_agent(self, session_id: str) -> Optional[BrowserAgentController]:
        return self.browser_agents.get(session_id)

    def get_browser_agent_status(self, session_id: str) -> dict:
        controller = self.browser_agents.get(session_id)
        if not controller:
            return BrowserAgentController.idle_payload()
        return controller.get_status()

    def get_browser_agent_state(self, session_id: str) -> dict:
        return self.get_browser_agent_status(session_id)

    async def enable_claude_code_auto_continue(self, session_id: str) -> bool:
        """Enable Claude Code usage-reset auto-continue for a session."""
        session = await self.get_session(session_id)
        controller = self.claude_code_auto_controllers.get(session_id)
        if not controller:
            controller = ClaudeCodeAutoContinue()
            controller.set_input_callback(lambda text: self._submit_automation_line(session_id, text))
            self.claude_code_auto_controllers[session_id] = controller

        controller.enable()
        controller.prime_with_output(session.get_recent_output())
        logger.info("Enabled Claude Code auto-continue for session %s", session_id)
        return True

    async def disable_claude_code_auto_continue(self, session_id: str):
        """Disable Claude Code usage-reset auto-continue for a session."""
        controller = self.claude_code_auto_controllers.get(session_id)
        if not controller:
            logger.debug("Claude Code auto-continue for session %s already disabled", session_id)
            return

        try:
            controller.disable()
        finally:
            self.claude_code_auto_controllers.pop(session_id, None)
            logger.info("Disabled Claude Code auto-continue for session %s", session_id)

    def get_claude_code_auto_continue(self, session_id: str) -> Optional[ClaudeCodeAutoContinue]:
        """Get Claude Code auto-continue controller for a session."""
        return self.claude_code_auto_controllers.get(session_id)

    async def close_session(self, session_id: str) -> None:
        """Close an active runtime session but keep its metadata entry."""
        session_exists = session_id in self.sessions
        ai_proxy_exists = session_id in self.ai_proxies
        browser_agent_exists = session_id in self.browser_agents
        claude_auto_exists = session_id in self.claude_code_auto_controllers

        if not session_exists and not ai_proxy_exists and not browser_agent_exists and not claude_auto_exists:
            logger.debug("Session %s already closed or doesn't exist", session_id)
            return

        logger.info(
            "Closing session %s (session_exists=%s, ai_proxy_exists=%s, browser_agent_exists=%s, claude_auto_exists=%s)",
            session_id,
            session_exists,
            ai_proxy_exists,
            browser_agent_exists,
            claude_auto_exists,
        )

        try:
            if ai_proxy_exists:
                try:
                    await self.disable_ai_proxy(session_id)
                except Exception as e:
                    logger.error("Error disabling AI proxy for session %s: %s", session_id, e)

            if browser_agent_exists:
                try:
                    await self.clear_browser_agent(session_id)
                except Exception as e:
                    logger.error("Error clearing browser agent for session %s: %s", session_id, e)

            if claude_auto_exists:
                try:
                    await self.disable_claude_code_auto_continue(session_id)
                except Exception as e:
                    logger.error("Error disabling Claude Code auto-continue for session %s: %s", session_id, e)

            if session_exists:
                try:
                    session = self.sessions[session_id]
                    await session.stop()
                    logger.debug("Successfully stopped terminal session %s", session_id)
                except Exception as e:
                    logger.error("Error stopping session %s: %s", session_id, e)
        finally:
            if session_exists:
                self.sessions.pop(session_id, None)
                logger.info("Closed active runtime for session %s, remaining sessions: %s", session_id, len(self.sessions))

    async def close_all(self) -> None:
        """Close all active runtime sessions."""
        session_ids = set(self.sessions.keys())
        session_ids.update(self.ai_proxies.keys())
        session_ids.update(self.browser_agents.keys())
        session_ids.update(self.claude_code_auto_controllers.keys())
        for session_id in session_ids:
            await self.close_session(session_id)
        logger.info("All sessions closed")

    def get_stats(self) -> dict:
        """Get session statistics."""
        return {
            "active_sessions": len(self.sessions),
            "max_sessions": self.max_sessions,
            "total_created": self.session_count,
            "ai_proxy_sessions": len(self.ai_proxies),
            "browser_agent_sessions": len(self.browser_agents),
        }

    def set_monitor_callback(self, callback):
        """Set monitoring callback for all AI proxies."""
        self.monitor_callback = callback
        for ai_proxy in self.ai_proxies.values():
            ai_proxy.set_monitor_callback(callback)
