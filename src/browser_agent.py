from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import inspect
import json
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from src.llm_provider import LLMProvider, LLMProviderFactory

logger = logging.getLogger(__name__)

BROWSER_AGENT_SYSTEM_PROMPT = '''You convert a user's browser ask-mode request into a short execution plan for a terminal agent.
Return strict JSON only with this shape:
{"summary": "short summary", "commands": [{"command": "shell command 1", "reason": "why"}]}
Rules:
- commands must be terminal commands only, in execution order
- prefer safe, incremental commands
- use an empty commands array if no terminal command is needed
- do not include markdown fences or commentary outside JSON'''
ASK_MODE_SYSTEM_PROMPT = BROWSER_AGENT_SYSTEM_PROMPT


@dataclass
class ProposedCommand:
    command: str
    state: str = 'pending'
    reason: Optional[str] = None


class BrowserAgentController:
    def __init__(
        self,
        *,
        llm_provider: Optional[LLMProvider] = None,
        provider_name: Optional[str] = None,
        system_prompt: Optional[str] = None,
        execute_command_callback: Optional[Callable[[str], Awaitable[None]]] = None,
        submit_command_callback: Optional[Callable[[str], Awaitable[None]]] = None,
        interrupt_command_callback: Optional[Callable[[], Awaitable[None]]] = None,
        session_id: Optional[str] = None,
    ) -> None:
        if llm_provider is None:
            resolved_provider = provider_name or 'gemini-cli'
            llm_provider = LLMProviderFactory.create(resolved_provider)
            if llm_provider is None:
                raise RuntimeError(f'Browser agent provider unavailable: {resolved_provider}')

        self.llm_provider = llm_provider
        self.provider_name = provider_name or llm_provider.get_name()
        self.system_prompt = system_prompt or BROWSER_AGENT_SYSTEM_PROMPT
        self.submit_command_callback = execute_command_callback or submit_command_callback
        if self.submit_command_callback is None:
            raise TypeError('execute_command_callback is required')
        self.interrupt_command_callback = interrupt_command_callback
        self.session_id = session_id
        self._state_callback: Optional[Callable[[dict], Awaitable[None] | None]] = None
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._reset_run_state(preserve_approvals=False)

    @staticmethod
    def idle_payload() -> dict:
        return {
            'status': 'idle',
            'prompt': '',
            'summary': '',
            'commands': [],
            'approval_mode': None,
            'pending_command_id': None,
            'error': None,
            'notice': None,
            'approvals': {
                'allow_all_for_session': False,
                'exact_commands': [],
                'patterns': [],
            },
        }

    def _reset_run_state(self, *, preserve_approvals: bool) -> None:
        exact = getattr(self, 'approved_exact_commands', set()) if preserve_approvals else set()
        wildcards = getattr(self, 'approved_wildcard_patterns', []) if preserve_approvals else []
        allow_all = getattr(self, 'allow_all_session', False) if preserve_approvals else False

        self.prompt: Optional[str] = None
        self.summary: str = ''
        self.state: str = 'idle'
        self.error: Optional[str] = None
        self.notice: Optional[str] = None
        self.commands: list[ProposedCommand] = []
        self.current_index: int = 0
        self.approval_mode_override: Optional[str] = None
        self.approved_exact_commands: set[str] = set(exact)
        self.approved_wildcard_patterns: list[str] = list(wildcards)
        self.allow_all_session: bool = allow_all
        self.force_command_review: bool = False

    def set_status_callback(self, callback: Optional[Callable[[dict], Awaitable[None] | None]]) -> None:
        self._state_callback = callback

    def set_state_callback(self, callback: Optional[Callable[[dict], Awaitable[None] | None]]) -> None:
        self.set_status_callback(callback)

    def get_status(self) -> dict:
        commands = [
            {
                'id': self._command_id(index),
                'command': item.command,
                'reason': item.reason,
                'state': item.state,
            }
            for index, item in enumerate(self.commands)
        ]
        return {
            'status': self.state,
            'prompt': self.prompt,
            'summary': self.summary,
            'commands': commands,
            'approval_mode': self._approval_mode(),
            'pending_command_id': self._command_id(self.current_index) if self.state == 'awaiting_approval' and self.current_index < len(self.commands) else None,
            'error': self.error,
            'notice': self.notice,
            'approvals': {
                'allow_all_for_session': self.allow_all_session,
                'exact_commands': sorted(self.approved_exact_commands),
                'patterns': list(self.approved_wildcard_patterns),
            },
        }

    def get_state(self) -> dict:
        return self.get_status()

    async def emit_status(self) -> None:
        if not self._state_callback:
            return
        result = self._state_callback(self.get_status())
        if inspect.isawaitable(result):
            await result

    async def submit_request(self, prompt: str, session_context: str = '', provider_name: Optional[str] = None) -> None:
        prompt = prompt.strip()
        if not prompt:
            raise RuntimeError('Prompt is required')

        async with self._lock:
            if self._task and not self._task.done():
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task

            self._reset_run_state(preserve_approvals=True)
            self.prompt = prompt
            if provider_name:
                provider = LLMProviderFactory.create(provider_name)
                if provider is None:
                    raise RuntimeError(f'Browser agent provider unavailable: {provider_name}')
                self.llm_provider = provider
                self.provider_name = provider_name
            self.state = 'thinking'
            self._task = asyncio.create_task(self._plan_run(session_context))

        await self.emit_status()

    async def submit(self, prompt: str, session_context: str = '', provider_name: Optional[str] = None) -> bool:
        if provider_name:
            self.provider_name = provider_name
        await self.submit_request(prompt, session_context=session_context, provider_name=provider_name)
        return True

    async def stop(self) -> None:
        async with self._lock:
            if self._task and not self._task.done():
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
            if self.interrupt_command_callback:
                with contextlib.suppress(Exception):
                    await self.interrupt_command_callback()
            if self.state not in {'idle', 'done', 'failed', 'stopped'}:
                self.state = 'stopped'
                self.notice = 'Agent run stopped by user'
        await self.emit_status()

    def clear_session_approvals(self) -> None:
        self.approved_exact_commands.clear()
        self.approved_wildcard_patterns.clear()
        self.allow_all_session = False

    async def clear(self) -> None:
        await self.stop()

    async def approve(
        self,
        *,
        scope: str,
        target: str = 'plan',
        command_ids: Optional[list[int | str]] = None,
        pattern: Optional[str] = None,
    ) -> None:
        async with self._lock:
            if self.state != 'awaiting_approval' or self.current_index >= len(self.commands):
                return

            if target not in {'plan', 'review', 'command'}:
                raise ValueError(f'Unknown approval target: {target}')

            if target == 'review':
                self.approval_mode_override = 'command'
                await self.emit_status()
                return

            indexes = self._selected_indexes(target=target, command_ids=command_ids)
            if not indexes:
                raise ValueError('No commands selected for approval')

            current_command = self.commands[self.current_index].command
            normalized_scope = self._normalize_scope(scope)
            if normalized_scope == 'all_session':
                self.allow_all_session = True
            elif normalized_scope == 'exact_session':
                for index in indexes:
                    self.approved_exact_commands.add(self.commands[index].command)
            elif normalized_scope == 'pattern_session':
                wildcard = (pattern or self._default_pattern_for(current_command)).strip()
                if not wildcard:
                    raise ValueError('Pattern approval requires a pattern')
                if wildcard not in self.approved_wildcard_patterns:
                    self.approved_wildcard_patterns.append(wildcard)
            elif normalized_scope != 'once':
                raise ValueError(f'Unknown approval scope: {scope}')

            run_plan = target == 'plan'
            self.force_command_review = not run_plan

        # Execute outside the lock — this can run an arbitrarily long shell
        # command, and holding the lock here would block stop() (and
        # submit_request()) from acquiring it, making a run impossible to
        # interrupt promptly.
        if run_plan:
            await self._execute_remaining(ignore_approval=True)
        else:
            await self._execute_current(ignore_approval=True)
            await self._advance_execution()

    async def approve_plan(self, scope: str) -> bool:
        await self.approve(scope=scope, target='plan')
        return True

    async def review_command_by_command(self) -> bool:
        async with self._lock:
            if self.state != 'awaiting_approval':
                return False
            self.approval_mode_override = 'command'
            self.notice = None
        await self.emit_status()
        return True

    async def approve_command(self, command_id: str, scope: str, *, pattern: Optional[str] = None) -> bool:
        await self.approve(scope=scope, target='command', command_ids=[command_id], pattern=pattern)
        return True

    async def _plan_run(self, session_context: str) -> None:
        try:
            prompt = self.prompt or ''
            if session_context.strip():
                prompt = f"{prompt}\n\nRecent terminal context:\n{session_context[-4000:]}"
            response = await self.llm_provider.generate(prompt, self.system_prompt)
            if not response or not response.is_success:
                self.state = 'failed'
                self.error = response.error_message or 'LLM provider failed'
                await self.emit_status()
                return

            summary, commands = self._parse_llm_response(response.text or '')
            self.summary = summary
            self.commands = commands
            if not self.commands:
                self.state = 'done'
                self.notice = 'LLM did not propose any terminal commands'
                await self.emit_status()
                return

            self.current_index = 0
            self.approval_mode_override = 'plan'
            await self._advance_execution()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception('Browser agent planning failed')
            self.state = 'failed'
            self.error = str(exc)
            await self.emit_status()

    async def _advance_execution(self) -> None:
        while self.current_index < len(self.commands):
            command = self.commands[self.current_index].command
            if self._is_approved(command):
                await self._execute_current(ignore_approval=False)
                continue
            self.state = 'awaiting_approval'
            if self.approval_mode_override not in {'plan', 'command'}:
                self.approval_mode_override = 'plan'
            self.error = None
            self.notice = None
            self.commands[self.current_index].state = 'awaiting_approval'
            await self.emit_status()
            return

        self.state = 'done'
        self.approval_mode_override = None
        self.notice = None
        await self.emit_status()

    async def _execute_remaining(self, *, ignore_approval: bool) -> None:
        while self.current_index < len(self.commands):
            await self._execute_current(ignore_approval=ignore_approval)
        self.state = 'done'
        self.approval_mode_override = None
        await self.emit_status()

    async def _execute_current(self, *, ignore_approval: bool) -> None:
        if self.current_index >= len(self.commands):
            return

        item = self.commands[self.current_index]
        if not ignore_approval and not self._is_approved(item.command):
            return

        item.state = 'executing'
        self.state = 'running'
        self.error = None
        self.notice = None
        await self.emit_status()
        await self.submit_command_callback(item.command)
        item.state = 'executed'
        self.current_index += 1
        await self.emit_status()

    def _is_approved(self, command: str) -> bool:
        if self.allow_all_session:
            return True
        if command in self.approved_exact_commands:
            return True
        return any(fnmatch.fnmatch(command, pattern) for pattern in self.approved_wildcard_patterns)

    def _selected_indexes(self, *, target: str, command_ids: Optional[list[int | str]]) -> list[int]:
        if target == 'plan':
            return list(range(self.current_index, len(self.commands)))

        if not command_ids:
            return [self.current_index]

        selected = sorted(
            index for index in (self._normalize_command_id(command_id) for command_id in command_ids)
            if index is not None and self.current_index <= index < len(self.commands)
        )
        if not selected:
            return []
        if selected[0] != self.current_index:
            raise ValueError('Only the current command can be approved next')
        return selected[:1]

    @staticmethod
    def _default_pattern_for(command: str) -> str:
        first_token = command.strip().split(maxsplit=1)[0] if command.strip() else ''
        return f'{first_token}*' if first_token else ''

    @staticmethod
    def _parse_llm_response(text: str) -> tuple[str, list[ProposedCommand]]:
        cleaned = text.strip()
        if cleaned.startswith('```'):
            cleaned = cleaned.strip('`')
            if cleaned.lower().startswith('json'):
                cleaned = cleaned[4:].strip()
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError:
            lines = [line.strip().lstrip('-0123456789. ') for line in cleaned.splitlines() if line.strip()]
            return cleaned, [ProposedCommand(command=line) for line in lines]

        if isinstance(payload, dict):
            summary = str(payload.get('summary') or '').strip()
            commands = BrowserAgentController._normalize_commands(payload.get('commands') or [])
            return summary, commands

        if isinstance(payload, list):
            return '', BrowserAgentController._normalize_commands(payload)

        return cleaned, []

    @staticmethod
    def _normalize_commands(commands: list[object]) -> list[ProposedCommand]:
        normalized: list[ProposedCommand] = []
        for item in commands:
            if isinstance(item, str):
                command = item.strip()
                reason = None
            elif isinstance(item, dict):
                command = str(item.get('command') or '').strip()
                reason = str(item.get('reason') or '').strip() or None
            else:
                command = str(item).strip()
                reason = None
            if command:
                normalized.append(ProposedCommand(command=command, reason=reason))
        return normalized

    def _approval_mode(self) -> Optional[str]:
        if self.state != 'awaiting_approval':
            return None
        if self.approval_mode_override:
            return self.approval_mode_override
        return 'plan' if len(self.commands) - self.current_index > 1 else 'command'

    @staticmethod
    def _normalize_scope(scope: str) -> str:
        mapping = {
            'command': 'exact_session',
            'exact': 'exact_session',
            'wildcard': 'pattern_session',
            'pattern': 'pattern_session',
            'session': 'all_session',
        }
        return mapping.get(scope, scope)

    @staticmethod
    def _command_id(index: int) -> str:
        return f'cmd-{index + 1}'

    @staticmethod
    def _normalize_command_id(command_id: int | str) -> Optional[int]:
        if isinstance(command_id, int):
            return command_id
        text = str(command_id)
        if text.startswith('cmd-'):
            try:
                return int(text.split('-', 1)[1]) - 1
            except ValueError:
                return None
        return None
