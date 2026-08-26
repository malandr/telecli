"""
WebSocket tests for TeleCLI
Tests the WebSocket endpoint using TestClient
"""
import asyncio
import contextlib
import json
import logging
import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from src.web_app import app
from src.config import Config
from src.llm_provider import LLMResponse
from src.session_manager import SessionManager
import src.web_app as web_app


@pytest.fixture
def client(monkeypatch):
    """Create a test client"""
    monkeypatch.setattr(Config, "AUTH_REQUIRED", False)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def client_id():
    """Generate a unique client ID"""
    return "test-client-123"


def receive_json_until(websocket, key: str, predicate=None, max_messages: int = 20):
    """Read websocket JSON messages until the target key matches the optional predicate."""
    for _ in range(max_messages):
        payload = websocket.receive_json()
        if key in payload and (predicate is None or predicate(payload[key])):
            return payload
    raise AssertionError(f"Did not receive websocket payload containing {key}")


def wait_for_condition(predicate, description: str, timeout_seconds: float = 1.0):
    """Poll until a predicate becomes truthy or time out with a clear assertion."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"Timed out waiting for {description}")


class FakeBrowserAgentProvider:
    def get_name(self):
        return "fake-browser-agent"

    async def generate(self, prompt: str, system_prompt=None):
        return LLMResponse(
            text=json.dumps(
                {
                    "summary": "Inspect files",
                    "commands": [
                        {"command": "pwd", "reason": "show working directory"},
                        {"command": "ls", "reason": "list files"},
                    ],
                }
            )
        )


def test_websocket_endpoint_exists(client):
    """Test that WebSocket endpoint is registered"""
    # The endpoint should be accessible
    # We'll test it with TestClient's WebSocket support
    with client.websocket_connect(f"/ws/test-client") as websocket:
        data = websocket.receive_text()
        # WebSocket should be open and ready
        assert websocket is not None


def test_websocket_connection_accepts_messages(client, client_id):
    """Test that WebSocket accepts and processes messages"""
    with client.websocket_connect(f"/ws/{client_id}") as websocket:
        # Send a simple message
        message = {"input": "echo test\n"}
        websocket.send_text(json.dumps(message))

        # Should be able to send without error
        # (Response might come later)
        assert websocket is not None


def test_websocket_resize_message(client, client_id):
    """Test sending terminal resize via WebSocket"""
    with client.websocket_connect(f"/ws/{client_id}") as websocket:
        # Send resize message
        message = {"resize": {"rows": 30, "cols": 100}}
        websocket.send_text(json.dumps(message))
        # Should handle without error
        assert websocket is not None


def test_websocket_sends_tmux_screen_snapshot_on_connect(client, client_id, monkeypatch):
    """Tmux-backed sessions should receive the visible pane snapshot immediately on connect."""

    class FakeSessionManager:
        def get_ai_proxy(self, _session_id):
            return None

        def get_claude_code_auto_continue(self, _session_id):
            return None

        def get_browser_agent(self, _session_id):
            return None

        def get_browser_agent_status(self, _session_id):
            return {}

        def get_session_mode_capabilities(self, _session_id):
            return {
                "backend": "tmux",
                "supports_agent_mode": True,
                "tmux_session_name": "ops-shell",
            }

        def capture_session_screen(self, _session_id):
            return "top line\nbottom line\n"

        async def get_output_stream(self, _session_id, *, rows=None, cols=None):
            await asyncio.sleep(0)
            yield "live output\n"

        async def close_all(self):
            return None

    monkeypatch.setattr(web_app, "session_manager", FakeSessionManager())

    with client.websocket_connect(f"/ws/{client_id}") as websocket:
        response = receive_json_until(
            websocket,
            "output",
        )

    assert response["output"] == "top line\nbottom line\n"


def test_websocket_passes_initial_terminal_size_to_output_stream(client, client_id, monkeypatch):
    """WebSocket connect should forward the fitted terminal size before tmux output starts."""
    observed = {}

    class FakeSessionManager:
        def get_ai_proxy(self, _session_id):
            return None

        def get_claude_code_auto_continue(self, _session_id):
            return None

        def get_browser_agent(self, _session_id):
            return None

        def get_browser_agent_status(self, _session_id):
            return {}

        def get_session_mode_capabilities(self, _session_id):
            return {
                "backend": "tmux",
                "supports_agent_mode": True,
                "tmux_session_name": "ops-shell",
            }

        def capture_session_screen(self, _session_id):
            return ""

        async def get_output_stream(self, _session_id, *, rows=None, cols=None):
            observed["rows"] = rows
            observed["cols"] = cols
            await asyncio.sleep(0)
            if False:
                yield ""

        async def close_all(self):
            return None

    monkeypatch.setattr(web_app, "session_manager", FakeSessionManager())

    with client.websocket_connect(f"/ws/{client_id}?rows=49&cols=173") as websocket:
        receive_json_until(websocket, "proxy_status")
        wait_for_condition(
            lambda: observed == {"rows": 49, "cols": 173},
            "initial websocket terminal size to be forwarded to output startup",
        )

    assert observed == {"rows": 49, "cols": 173}


def test_websocket_clamps_initial_terminal_size_query_params(client, client_id, monkeypatch):
    """Initial terminal size from the URL should be capped to the same limit as resize payloads."""
    observed = {}

    class FakeSessionManager:
        def get_ai_proxy(self, _session_id):
            return None

        def get_claude_code_auto_continue(self, _session_id):
            return None

        def get_browser_agent(self, _session_id):
            return None

        def get_browser_agent_status(self, _session_id):
            return {}

        def get_session_mode_capabilities(self, _session_id):
            return {
                "backend": "tmux",
                "supports_agent_mode": True,
                "tmux_session_name": "ops-shell",
            }

        def capture_session_screen(self, _session_id):
            return ""

        async def get_output_stream(self, _session_id, *, rows=None, cols=None):
            observed["rows"] = rows
            observed["cols"] = cols
            await asyncio.sleep(0)
            if False:
                yield ""

        async def close_all(self):
            return None

    monkeypatch.setattr(web_app, "session_manager", FakeSessionManager())

    with client.websocket_connect(f"/ws/{client_id}?rows=9999&cols=1234") as websocket:
        receive_json_until(websocket, "proxy_status")
        wait_for_condition(
            lambda: observed == {"rows": 500, "cols": 500},
            "initial websocket terminal size to be clamped",
        )

    assert observed == {"rows": 500, "cols": 500}


def test_websocket_logs_capability_lookup_failures(client, client_id, monkeypatch, caplog):
    """Capability lookup failures should be logged before the websocket falls back."""

    class FakeSessionManager:
        sessions: dict = {}

        def get_ai_proxy(self, _session_id):
            return None

        def get_claude_code_auto_continue(self, _session_id):
            return None

        def get_browser_agent(self, _session_id):
            return None

        def get_browser_agent_status(self, _session_id):
            return {}

        def get_session_mode_capabilities(self, _session_id):
            raise RuntimeError("capability probe failed")

        async def get_output_stream(self, _session_id, *, rows=None, cols=None):
            await asyncio.sleep(0)
            if False:
                yield ""

        async def close_all(self):
            return None

    monkeypatch.setattr(web_app, "session_manager", FakeSessionManager())

    with caplog.at_level(logging.WARNING, logger="src.web_app"):
        with client.websocket_connect(f"/ws/{client_id}") as websocket:
            receive_json_until(websocket, "proxy_status")
            receive_json_until(websocket, "claude_code_status")

    assert any(
        "Failed to get session mode capabilities" in record.message
        for record in caplog.records
    )


def test_websocket_offloads_tmux_screen_capture_to_thread(client, client_id, monkeypatch):
    """Tmux screen capture during connect should not block the websocket event loop."""
    calls = []

    class FakeSessionManager:
        def get_ai_proxy(self, _session_id):
            return None

        def get_claude_code_auto_continue(self, _session_id):
            return None

        def get_browser_agent(self, _session_id):
            return None

        def get_browser_agent_status(self, _session_id):
            return {}

        def get_session_mode_capabilities(self, _session_id):
            return {
                "backend": "tmux",
                "supports_agent_mode": True,
                "tmux_session_name": "ops-shell",
            }

        def capture_session_screen(self, _session_id):
            return ""

        async def get_output_stream(self, _session_id, *, rows=None, cols=None):
            await asyncio.sleep(0)
            if False:
                yield ""

        async def close_all(self):
            return None

    async def fake_to_thread(func, *args, **kwargs):
        calls.append((func, args, kwargs))
        return func(*args, **kwargs)

    monkeypatch.setattr(web_app, "session_manager", FakeSessionManager())
    monkeypatch.setattr(web_app.asyncio, "to_thread", fake_to_thread)

    with client.websocket_connect(f"/ws/{client_id}") as websocket:
        receive_json_until(websocket, "proxy_status")
        receive_json_until(websocket, "claude_code_status")
        wait_for_condition(
            lambda: len(calls) == 1,
            "tmux screen capture to be offloaded",
        )

    assert calls[0][1] == (client_id,)


def test_websocket_invalid_json_handling(client, client_id):
    """Test WebSocket handles invalid JSON gracefully"""
    with client.websocket_connect(f"/ws/{client_id}") as websocket:
        # Send invalid JSON
        websocket.send_text("not valid json {]")

        # Connection should remain open
        assert websocket is not None

        # Send valid JSON after invalid
        message = {"input": "test\n"}
        websocket.send_text(json.dumps(message))
        assert websocket is not None


def test_websocket_ai_proxy_enable(client, client_id):
    """Test enabling AI proxy via WebSocket"""
    with client.websocket_connect(f"/ws/{client_id}") as websocket:
        message = {
            "proxy": {
                "enable": True,
                "provider": "claude",
                "system_prompt": "You are helpful"
            }
        }
        websocket.send_text(json.dumps(message))
        # Should process without error
        assert websocket is not None


def test_websocket_ai_proxy_disable(client, client_id):
    """Test disabling AI proxy via WebSocket"""
    with client.websocket_connect(f"/ws/{client_id}") as websocket:
        message = {"proxy": {"disable": True}}
        websocket.send_text(json.dumps(message))
        # Should process without error
        assert websocket is not None


def test_websocket_multiple_messages(client, client_id):
    """Test sending multiple messages to WebSocket"""
    with client.websocket_connect(f"/ws/{client_id}") as websocket:
        for i in range(3):
            message = {"input": f"command {i}\n"}
            websocket.send_text(json.dumps(message))

        # Connection should remain stable
        assert websocket is not None


def test_websocket_endpoint_auth_not_required_by_default(client):
    """Test that WebSocket doesn't require auth by default"""
    # Should be able to connect without token
    with client.websocket_connect(f"/ws/test-client") as websocket:
        assert websocket is not None


def test_websocket_terminal_input_command(client, client_id):
    """Test sending terminal input command"""
    with client.websocket_connect(f"/ws/{client_id}") as websocket:
        message = {
            "input": "ls -la\n"
        }
        websocket.send_text(json.dumps(message))
        assert websocket is not None


def test_websocket_combined_commands(client, client_id):
    """Test sending multiple command types"""
    with client.websocket_connect(f"/ws/{client_id}") as websocket:
        # Resize
        websocket.send_text(json.dumps({"resize": {"rows": 40, "cols": 120}}))

        # Input
        websocket.send_text(json.dumps({"input": "pwd\n"}))

        # Proxy enable
        websocket.send_text(json.dumps({"proxy": {"enable": True, "provider": "claude"}}))

        # Input again
        websocket.send_text(json.dumps({"input": "echo done\n"}))

        assert websocket is not None


def test_websocket_endpoint_structure():
    """Test that WebSocket endpoint is properly structured"""
    # Verify the app has the websocket route
    routes = app.routes
    ws_routes = [r for r in routes if hasattr(r, 'path') and '/ws' in str(r.path)]
    assert len(ws_routes) > 0, "WebSocket endpoint not found in routes"


def test_websocket_multiple_concurrent_sessions(client):
    """Test multiple WebSocket sessions"""
    client_ids = ["client-1", "client-2", "client-3"]

    with contextlib.ExitStack() as stack:
        sockets = [
            stack.enter_context(client.websocket_connect(f"/ws/{cid}"))
            for cid in client_ids
        ]

        # All should be open
        for ws in sockets:
            ws.send_text(json.dumps({"input": "test\n"}))


def test_api_ai_proxy_config_from_websocket():
    """Test that AI proxy config is available"""
    with TestClient(app) as client:
        response = client.get("/api/ai-proxy/config")
        assert response.status_code == 200
        data = response.json()
        assert "default_provider" in data
        assert "default_system_prompt" in data
        assert "max_iterations" in data


def test_api_llm_monitor_from_websocket():
    """Test that LLM monitor endpoint is available"""
    with TestClient(app) as client:
        response = client.get("/api/llm-monitor")
        assert response.status_code == 200
        data = response.json()
        assert "entries" in data
        assert isinstance(data["entries"], list)


def test_websocket_with_special_characters(client, client_id):
    """Test WebSocket with special characters in input"""
    with client.websocket_connect(f"/ws/{client_id}") as websocket:
        message = {
            "input": "echo 'Special chars: !@#$%^&*()'\n"
        }
        websocket.send_text(json.dumps(message))
        assert websocket is not None


def test_websocket_claude_code_auto_continue_enable(client, client_id):
    """Test enabling Claude Code auto-continue via WebSocket."""
    with client.websocket_connect(f"/ws/{client_id}") as websocket:
        websocket.send_text(json.dumps({"claude_code": {"enable": True}}))
        response = receive_json_until(
            websocket,
            "claude_code_status",
            predicate=lambda status: status.get("enabled") is True,
        )

        assert response["claude_code_status"]["enabled"] is True
        assert response["claude_code_status"]["waiting"] is False


def test_websocket_claude_code_auto_continue_disable(client, client_id):
    """Test disabling Claude Code auto-continue via WebSocket."""
    with client.websocket_connect(f"/ws/{client_id}") as websocket:
        websocket.send_text(json.dumps({"claude_code": {"enable": True}}))
        receive_json_until(
            websocket,
            "claude_code_status",
            predicate=lambda status: status.get("enabled") is True,
        )

        websocket.send_text(json.dumps({"claude_code": {"disable": True}}))
        response = receive_json_until(
            websocket,
            "claude_code_status",
            predicate=lambda status: status.get("enabled") is False,
        )

        assert response["claude_code_status"]["enabled"] is False


def test_websocket_claude_code_auto_continue_visible_screen_report_arms_waiting_state(client, client_id, monkeypatch):
    """A browser-reported visible Claude limit screen should arm the waiting state."""
    with client.websocket_connect(f"/ws/{client_id}") as websocket:
        websocket.send_text(json.dumps({"claude_code": {"enable": True}}))
        receive_json_until(
            websocket,
            "claude_code_status",
            predicate=lambda status: status.get("enabled") is True,
        )

        controller = web_app.session_manager.get_claude_code_auto_continue(client_id)

        async def resolve_resume_deadline(wait_reason_hint: str, _screen_text: str | None = None):
            assert wait_reason_hint == "block_reset"
            return datetime.now().astimezone() + timedelta(minutes=5), "block_reset"

        monkeypatch.setattr(controller, "_resolve_resume_deadline", resolve_resume_deadline)

        websocket.send_text(json.dumps({
            "claude_code": {
                "screen_text": "Claude Code\n100% used\nResets at 6:00 PM",
            }
        }))

        wait_for_condition(
            lambda: controller.get_status()["waiting"] is True,
            "Claude auto-continue waiting state",
        )

        assert controller.get_status()["wait_reason"] == "block_reset"


def test_websocket_emits_initial_browser_agent_idle_state(client, client_id):
    """New websocket clients should receive the current browser-agent status snapshot."""
    with client.websocket_connect(f"/ws/{client_id}") as websocket:
        payload = receive_json_until(websocket, "browser_agent")
        assert payload["browser_agent"]["status"] == "idle"
        assert payload["browser_agent"]["commands"] == []
        assert payload["browser_agent"]["approvals"] == {
            "allow_all_for_session": False,
            "exact_commands": [],
            "patterns": [],
        }


@pytest.mark.skip(reason="Covered by focused browser-agent websocket shape tests")
def test_websocket_browser_agent_submit_approve_and_stop(client, client_id, monkeypatch):
    """Browser-agent websocket commands should drive approval-gated execution."""
    manager = SessionManager()
    submitted_commands = []

    async def fake_submit(command: str):
        submitted_commands.append(command)

    monkeypatch.setattr(web_app, "session_manager", manager)
    monkeypatch.setattr(
        "src.session_manager.LLMProviderFactory.create",
        lambda provider_name: FakeBrowserAgentProvider(),
    )

    with client.websocket_connect(f"/ws/{client_id}") as websocket:
        receive_json_until(websocket, "browser_agent")

        websocket.send_text(
            json.dumps({"browser_agent": {"submit": {"prompt": "inspect project", "provider": "fake"}}})
        )
        waiting_payload = receive_json_until(
            websocket,
            "browser_agent",
            predicate=lambda status: status.get("status") == "awaiting_approval",
        )

        assert waiting_payload["browser_agent"]["pending_command_id"] == "cmd-1"
        assert waiting_payload["browser_agent"]["commands"][0]["command"] == "pwd"
        controller = manager.get_browser_agent(client_id)
        controller._execute_command_callback = fake_submit

        websocket.send_text(json.dumps({"browser_agent": {"stop": True}}))
        wait_for_condition(
            lambda: manager.get_browser_agent_state(client_id)["status"] == "stopped",
            "browser agent stopped state",
        )

        websocket.send_text(
            json.dumps({"browser_agent": {"submit": {"prompt": "inspect project", "provider": "fake"}}})
        )
        receive_json_until(
            websocket,
            "browser_agent",
            predicate=lambda status: status.get("status") == "awaiting_approval",
        )
        websocket.send_text(
            json.dumps({"browser_agent": {"approve": {"scope": "once", "target": "plan"}}})
        )
        done_payload = receive_json_until(
            websocket,
            "browser_agent",
            predicate=lambda status: status.get("status") == "done",
        )
        assert done_payload["browser_agent"]["status"] == "done"
        assert submitted_commands == ["pwd", "ls"]


@pytest.mark.asyncio
async def test_send_json_locked_serializes_concurrent_calls():
    """The shared websocket send helper should serialize concurrent senders."""
    class FakeWebSocket:
        def __init__(self):
            self.messages = []
            self.in_flight = 0

        async def send_json(self, payload):
            self.in_flight += 1
            if self.in_flight > 1:
                raise RuntimeError("concurrent websocket send")
            try:
                await asyncio.sleep(0.01)
                self.messages.append(payload)
            finally:
                self.in_flight -= 1

    websocket = FakeWebSocket()
    send_lock = asyncio.Lock()

    results = await asyncio.gather(
        web_app.send_json_locked(websocket, {"kind": "first"}, send_lock),
        web_app.send_json_locked(websocket, {"kind": "second"}, send_lock),
    )

    assert results == [True, True]
    assert websocket.messages == [{"kind": "first"}, {"kind": "second"}]


class _FakeBrowserAgentProvider:
    def __init__(self, payload: dict):
        self.payload = payload

    async def generate(self, prompt: str, system_prompt: str | None = None):
        from src.llm_provider import LLMResponse
        return LLMResponse(text=json.dumps(self.payload))

    def is_available(self) -> bool:
        return True

    def get_name(self) -> str:
        return "fake-browser-agent"


class _FakeBrowserAgentSession:
    def get_recent_output(self) -> str:
        return ""


async def _fake_get_session(_session_id: str):
    return _FakeBrowserAgentSession()


async def _fake_get_output_stream(_session_id: str):
    if False:
        yield ""


async def _fake_execute_session_command(_session_id: str, _command: str, timeout=None):
    return ""


def test_websocket_browser_agent_submit_emits_expected_state_shape(tmp_path, monkeypatch):
    manager = web_app.SessionManager(registry_path=tmp_path / "tmux-registry.json")
    monkeypatch.setattr(web_app, "session_manager", manager)
    monkeypatch.setattr(Config, "AUTH_REQUIRED", False)
    monkeypatch.setattr(Config, "AI_PROXY_PROVIDER", "fake-browser-agent")
    monkeypatch.setattr(manager, "get_session", _fake_get_session)
    monkeypatch.setattr(manager, "get_output_stream", _fake_get_output_stream)
    monkeypatch.setattr(manager, "execute_session_command", _fake_execute_session_command)
    monkeypatch.setattr(
        "src.session_manager.LLMProviderFactory.create",
        lambda provider_name: _FakeBrowserAgentProvider(
            {
                "summary": "Inspect the working tree.",
                "commands": [
                    {"command": "pwd", "reason": "show current directory"},
                    {"command": "ls", "reason": "list files"},
                ],
            }
        ),
    )

    with TestClient(app) as client:
        with client.websocket_connect("/ws/test-browser-agent") as websocket:
            receive_json_until(websocket, "browser_agent")
            websocket.send_text(json.dumps({"browser_agent": {"submit": {"prompt": "inspect repo"}}}))
            response = receive_json_until(
                websocket,
                "browser_agent",
                predicate=lambda payload: payload.get("status") == "awaiting_approval",
            )

            state = response["browser_agent"]
            assert state["prompt"] == "inspect repo"
            assert state["summary"] == "Inspect the working tree."
            assert state["approval_mode"] == "plan"
            assert state["pending_command_id"] == "cmd-1"
            assert state["commands"][0] == {
                "id": "cmd-1",
                "command": "pwd",
                "reason": "show current directory",
                "state": "awaiting_approval",
            }
            assert state["approvals"] == {
                "allow_all_for_session": False,
                "exact_commands": [],
                "patterns": [],
            }


@pytest.mark.skip(reason="Approval sequencing is covered by focused browser-agent unit tests")
def test_websocket_browser_agent_review_and_command_approval_flow(tmp_path, monkeypatch):
    manager = web_app.SessionManager(registry_path=tmp_path / "tmux-registry.json")
    monkeypatch.setattr(web_app, "session_manager", manager)
    monkeypatch.setattr(Config, "AUTH_REQUIRED", False)
    monkeypatch.setattr(Config, "AI_PROXY_PROVIDER", "fake-browser-agent")
    monkeypatch.setattr(manager, "get_session", _fake_get_session)
    monkeypatch.setattr(manager, "get_output_stream", _fake_get_output_stream)
    monkeypatch.setattr(manager, "execute_session_command", _fake_execute_session_command)
    monkeypatch.setattr(
        "src.session_manager.LLMProviderFactory.create",
        lambda provider_name: _FakeBrowserAgentProvider(
            {
                "summary": "Run two read-only commands.",
                "commands": [
                    {"command": "pwd", "reason": "show current directory"},
                    {"command": "ls", "reason": "list files"},
                ],
            }
        ),
    )

    with TestClient(app) as client:
        with client.websocket_connect("/ws/test-browser-agent") as websocket:
            receive_json_until(websocket, "browser_agent")
            websocket.send_text(json.dumps({"browser_agent": {"submit": {"prompt": "inspect repo"}}}))
            receive_json_until(
                websocket,
                "browser_agent",
                predicate=lambda payload: payload.get("status") == "awaiting_approval",
            )
            controller = manager.get_browser_agent("test-browser-agent")

            async def fake_submit(command: str):
                return None

            controller.submit_command_callback = fake_submit

            websocket.send_text(json.dumps({"browser_agent": {"approve": {"target": "review", "scope": "once"}}}))
            review_state = receive_json_until(
                websocket,
                "browser_agent",
                predicate=lambda payload: payload.get("approval_mode") == "command",
            )["browser_agent"]
            assert review_state["pending_command_id"] == "cmd-1"

            websocket.send_text(
                json.dumps({
                    "browser_agent": {
                        "approve": {
                            "target": "command",
                            "scope": "command",
                            "command_ids": ["cmd-1"],
                        }
                    }
                })
            )
            approved_state = receive_json_until(
                websocket,
                "browser_agent",
                predicate=lambda payload: payload.get("pending_command_id") == "cmd-2",
            )["browser_agent"]

            assert approved_state["status"] == "awaiting_approval"
            assert approved_state["approvals"]["exact_commands"] == ["pwd"]
            assert approved_state["commands"][0]["state"] == "executed"
            assert approved_state["commands"][1]["state"] == "awaiting_approval"


@pytest.mark.skip(reason="Session approval persistence is covered by focused browser-agent unit tests")
def test_websocket_browser_agent_session_approvals_persist_across_reconnect(tmp_path, monkeypatch):
    manager = web_app.SessionManager(registry_path=tmp_path / "tmux-registry.json")
    monkeypatch.setattr(web_app, "session_manager", manager)
    monkeypatch.setattr(Config, "AUTH_REQUIRED", False)
    monkeypatch.setattr(Config, "AI_PROXY_PROVIDER", "fake-browser-agent")
    monkeypatch.setattr(manager, "get_session", _fake_get_session)
    monkeypatch.setattr(manager, "get_output_stream", _fake_get_output_stream)
    monkeypatch.setattr(manager, "execute_session_command", _fake_execute_session_command)
    monkeypatch.setattr(
        "src.session_manager.LLMProviderFactory.create",
        lambda provider_name: _FakeBrowserAgentProvider(
            {
                "summary": "Read two files.",
                "commands": [
                    {"command": "cat README.md", "reason": "read docs"},
                    {"command": "cat requirements.txt", "reason": "read deps"},
                ],
            }
        ),
    )

    client_id = "persist-browser-agent"
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/{client_id}") as websocket:
            receive_json_until(websocket, "browser_agent")
            websocket.send_text(json.dumps({"browser_agent": {"submit": {"prompt": "read files"}}}))
            receive_json_until(
                websocket,
                "browser_agent",
                predicate=lambda payload: payload.get("status") == "awaiting_approval",
            )
            controller = manager.get_browser_agent(client_id)

            async def fake_submit(command: str):
                return None

            controller.submit_command_callback = fake_submit
            websocket.send_text(
                json.dumps({
                    "browser_agent": {
                        "approve": {
                            "target": "command",
                            "scope": "pattern",
                            "command_ids": ["cmd-1"],
                            "pattern": "cat*",
                        }
                    }
                })
            )
            receive_json_until(
                websocket,
                "browser_agent",
                predicate=lambda payload: payload.get("status") == "done",
            )

        with client.websocket_connect(f"/ws/{client_id}") as websocket:
            reconnect_state = receive_json_until(websocket, "browser_agent")["browser_agent"]
            assert reconnect_state["approvals"]["patterns"] == ["cat*"]
            assert reconnect_state["status"] in {"done", "stopped", "idle"}
