import asyncio
import json

import pytest

from src.browser_agent import BrowserAgentController
from src.llm_provider import LLMResponse


class FakeProvider:
    def __init__(self, payload: dict):
        self.payload = payload

    async def generate(self, prompt: str, system_prompt=None) -> LLMResponse:
        return LLMResponse(text=json.dumps(self.payload))

    def get_name(self) -> str:
        return "fake-provider"


@pytest.mark.asyncio
async def test_browser_agent_submit_exposes_plan_status():
    executed = []
    controller = BrowserAgentController(
        llm_provider=FakeProvider(
            {
                "summary": "List files",
                "commands": [
                    {"command": "pwd", "reason": "Confirm location"},
                    {"command": "ls", "reason": "Inspect files"},
                ],
            }
        ),
        execute_command_callback=lambda command: executed.append(command) or asyncio.sleep(0),
    )

    await controller.submit_request("show me the repo")
    await asyncio.wait_for(controller._task, timeout=1)

    status = controller.get_status()
    assert status["status"] == "awaiting_approval"
    assert status["approval_mode"] == "plan"
    assert status["summary"] == "List files"
    assert [command["command"] for command in status["commands"]] == ["pwd", "ls"]
    assert executed == []


@pytest.mark.asyncio
async def test_browser_agent_plan_once_executes_all_commands():
    executed = []

    async def execute(command: str) -> None:
        executed.append(command)

    controller = BrowserAgentController(
        llm_provider=FakeProvider(
            {
                "summary": "Inspect",
                "commands": [{"command": "pwd"}, {"command": "ls"}],
            }
        ),
        execute_command_callback=execute,
    )

    await controller.submit_request("inspect repo")
    await asyncio.wait_for(controller._task, timeout=1)
    await controller.approve(scope="once", target="plan")

    status = controller.get_status()
    assert executed == ["pwd", "ls"]
    assert status["status"] == "done"
    assert all(command["state"] == "executed" for command in status["commands"])


@pytest.mark.asyncio
async def test_stop_interrupts_promptly_during_long_running_approved_execution():
    """approve() must not hold the controller lock for the duration of command
    execution, or stop() (and submit_request()) can't acquire it to interrupt
    a run that's stuck on a long-running command."""
    command_started = asyncio.Event()
    release_command = asyncio.Event()
    interrupted = []

    async def execute(command: str) -> None:
        command_started.set()
        await release_command.wait()

    async def interrupt() -> None:
        interrupted.append(True)
        release_command.set()

    controller = BrowserAgentController(
        llm_provider=FakeProvider(
            {
                "summary": "Long task",
                "commands": [{"command": "sleep 100"}],
            }
        ),
        execute_command_callback=execute,
        interrupt_command_callback=interrupt,
    )

    await controller.submit_request("run something slow")
    await asyncio.wait_for(controller._task, timeout=1)

    approve_task = asyncio.create_task(controller.approve(scope="once", target="plan"))
    await asyncio.wait_for(command_started.wait(), timeout=1)

    # The lock must be free here even though approve_task is still awaiting
    # the (blocked) command execution — this hangs before the fix.
    await asyncio.wait_for(controller.stop(), timeout=1)

    assert interrupted == [True]
    assert controller.get_status()["status"] == "stopped"

    await asyncio.wait_for(approve_task, timeout=1)


@pytest.mark.asyncio
async def test_browser_agent_pattern_approval_persists_for_session():
    executed = []

    async def execute(command: str) -> None:
        executed.append(command)

    controller = BrowserAgentController(
        llm_provider=FakeProvider(
            {
                "summary": "Read files",
                "commands": [
                    {"command": "cat README.md"},
                    {"command": "cat requirements.txt"},
                ],
            }
        ),
        execute_command_callback=execute,
    )

    await controller.submit_request("read docs")
    await asyncio.wait_for(controller._task, timeout=1)
    await controller.approve(scope="pattern", target="command", command_ids=[0], pattern="cat*")

    status = controller.get_status()
    assert executed == ["cat README.md", "cat requirements.txt"]
    assert status["status"] == "done"
    assert status["approvals"]["patterns"] == ["cat*"]
