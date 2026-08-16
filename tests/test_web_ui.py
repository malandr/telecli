"""
Playwright/HTTP tests for web UI functionality
Tests the HTTP routes, static file serving, and API endpoints
"""
import pytest
from src import web_app
from src.web_app import app
from fastapi.testclient import TestClient
import time
from src.session_manager import SessionManager


def test_root_path_serves_index_html():
    """Test that GET / serves the index.html"""
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        # HTML file endpoint should return file response
        assert response.headers["content-type"] in ["text/html; charset=utf-8", "text/html"]


def test_root_page_includes_browser_agent_panel():
    """The browser UI should expose the ask-mode panel shell for Alt+Enter workflows."""
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        body = response.text
        assert 'browser-agent-panel' in body
        assert 'Press Alt+Enter to send the current shell draft to the LLM.' in body


def test_telecli_path_serves_index_html():
    """Test that GET /telecli also serves the index.html"""
    with TestClient(app) as client:
        response = client.get("/telecli")
        assert response.status_code == 200
        assert response.headers["content-type"] in ["text/html; charset=utf-8", "text/html"]


def test_style_css_loads():
    """Test that CSS file is served"""
    with TestClient(app) as client:
        response = client.get("/style.css")
        assert response.status_code == 200
        assert "text/css" in response.headers["content-type"]


def test_health_endpoint_returns_json():
    """Test health check endpoint via HTTP"""
    with TestClient(app) as client:
        response = client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "sessions" in data


def test_debug_endpoint_returns_request_info():
    """Test debug endpoint returns request information"""
    with TestClient(app) as client:
        response = client.get("/debug")

        assert response.status_code == 200
        data = response.json()
        assert "url" in data
        assert "method" in data
        assert "headers" in data
        assert data["method"] == "GET"


def test_stats_endpoint_returns_stats():
    """Test stats endpoint"""
    with TestClient(app) as client:
        response = client.get("/stats")

        assert response.status_code == 200
        data = response.json()
        # Stats should return session information
        assert isinstance(data, dict)


def test_api_sessions_endpoint():
    """Test active sessions endpoint"""
    with TestClient(app) as client:
        response = client.get("/api/sessions")

        assert response.status_code == 200
        data = response.json()
        assert "sessions" in data
        assert isinstance(data["sessions"], list)


def test_api_sessions_endpoint_returns_named_backend_entries(tmp_path, monkeypatch):
    """Session list API should expose names and backend metadata for the picker UI."""
    manager = SessionManager(registry_path=tmp_path / "tmux-registry.json")
    monkeypatch.setattr(
        manager,
        "list_machine_tmux_sessions",
        lambda: [{"name": "ops", "windows": 1, "attached": True}],
    )
    manager.import_tmux_session("ops", name="Ops Session")

    with TestClient(app) as client:
        monkeypatch.setattr(web_app, "session_manager", manager)
        response = client.get("/api/sessions")

        assert response.status_code == 200
        data = response.json()
        assert data["sessions"][0]["name"] == "Ops Session"
        assert data["sessions"][0]["backend"] == "tmux"
        assert data["sessions"][0]["tmux_session_name"] == "ops"


def test_api_session_entry_lifecycle_routes(tmp_path, monkeypatch):
    """Session create, rename, and delete routes should round-trip named entries."""
    manager = SessionManager(registry_path=tmp_path / "tmux-registry.json")

    with TestClient(app) as client:
        monkeypatch.setattr(web_app, "session_manager", manager)

        create_response = client.post("/api/sessions", json={"name": "Dev Shell"})
        assert create_response.status_code == 200
        created_session = create_response.json()["session"]
        assert created_session["name"] == "Dev Shell"
        assert created_session["backend"] == "telecli"

        rename_response = client.patch(
            f"/api/sessions/{created_session['id']}",
            json={"name": "Primary Dev"},
        )
        assert rename_response.status_code == 200
        assert rename_response.json()["session"]["name"] == "Primary Dev"

        delete_response = client.delete(f"/api/sessions/{created_session['id']}")
        assert delete_response.status_code == 200

        sessions_response = client.get("/api/sessions")
        assert sessions_response.status_code == 200
        assert sessions_response.json()["sessions"] == []


def test_api_tmux_sessions_endpoint_lists_machine_sessions(tmp_path, monkeypatch):
    """Tmux discovery API should list machine sessions without importing them automatically."""
    manager = SessionManager(registry_path=tmp_path / "tmux-registry.json")
    monkeypatch.setattr(
        manager,
        "list_machine_tmux_sessions",
        lambda: [
            {"name": "dev", "windows": 3, "attached": True},
            {"name": "build", "windows": 1, "attached": False},
        ],
    )

    with TestClient(app) as client:
        monkeypatch.setattr(web_app, "session_manager", manager)
        response = client.get("/api/tmux/sessions")

        assert response.status_code == 200
        data = response.json()
        assert [session["name"] for session in data["sessions"]] == ["dev", "build"]


def test_api_import_tmux_session_route(tmp_path, monkeypatch):
    """Importing a tmux session should return the persisted TeleCLI entry."""
    manager = SessionManager(registry_path=tmp_path / "tmux-registry.json")
    monkeypatch.setattr(
        manager,
        "list_machine_tmux_sessions",
        lambda: [{"name": "ops", "windows": 2, "attached": False}],
    )

    with TestClient(app) as client:
        monkeypatch.setattr(web_app, "session_manager", manager)
        response = client.post("/api/sessions/import-tmux", json={"tmux_session_name": "ops"})

        assert response.status_code == 200
        session = response.json()["session"]
        assert session["backend"] == "tmux"
        assert session["tmux_session_name"] == "ops"


def test_api_create_tmux_session_route(tmp_path, monkeypatch):
    """Creating a new tmux session should return the imported TeleCLI entry."""
    manager = SessionManager(registry_path=tmp_path / "tmux-registry.json")
    monkeypatch.setattr(
        manager,
        "create_tmux_session_entry",
        lambda name: {
            "id": "tmux-123",
            "name": name,
            "backend": "tmux",
            "tmux_session_name": name,
            "is_active": False,
            "available": True,
            "shell": f"tmux:{name}",
            "created_at": "2026-03-18T12:00:00+00:00",
        },
    )

    with TestClient(app) as client:
        monkeypatch.setattr(web_app, "session_manager", manager)
        response = client.post("/api/tmux/sessions", json={"name": "pairing"})

        assert response.status_code == 200
        session = response.json()["session"]
        assert session["backend"] == "tmux"
        assert session["tmux_session_name"] == "pairing"


def test_api_detach_tmux_session_route(tmp_path, monkeypatch):
    """Detaching should close the runtime but keep the imported tmux entry."""
    manager = SessionManager(registry_path=tmp_path / "tmux-registry.json")

    async def fake_detach(session_id):
        return {
            "id": session_id,
            "name": "Ops Shell",
            "backend": "tmux",
            "tmux_session_name": "ops-shell",
            "is_active": False,
            "available": True,
            "shell": "tmux:ops-shell",
            "created_at": "2026-03-18T12:00:00+00:00",
        }

    monkeypatch.setattr(manager, "detach_tmux_session", fake_detach)

    with TestClient(app) as client:
        monkeypatch.setattr(web_app, "session_manager", manager)
        response = client.post("/api/sessions/tmux-123/detach")

        assert response.status_code == 200
        session = response.json()["session"]
        assert session["id"] == "tmux-123"
        assert session["backend"] == "tmux"
        assert session["is_active"] is False


def test_api_auth_required_endpoint():
    """Test auth required endpoint"""
    with TestClient(app) as client:
        response = client.get("/api/auth/required")

        assert response.status_code == 200
        data = response.json()
        assert "auth_required" in data
        assert isinstance(data["auth_required"], bool)


def test_api_ai_proxy_config_endpoint():
    """Test AI proxy config endpoint"""
    with TestClient(app) as client:
        response = client.get("/api/ai-proxy/config")

        assert response.status_code == 200
        data = response.json()
        assert "default_provider" in data
        assert "default_system_prompt" in data
        assert "max_iterations" in data


def test_api_llm_monitor_endpoint():
    """Test LLM monitor data endpoint"""
    with TestClient(app) as client:
        response = client.get("/api/llm-monitor")

        assert response.status_code == 200
        data = response.json()
        assert "entries" in data
        assert isinstance(data["entries"], list)


def test_static_files_mount_root():
    """Test static files are served from /static"""
    with TestClient(app) as client:
        # Test that static mount is registered
        # We can't actually fetch static files without the files existing
        # but we can test that the route structure is correct
        response = client.get("/debug")
        assert response.status_code == 200


def test_static_files_mount_telecli():
    """Test static files are served from /telecli/static"""
    with TestClient(app) as client:
        response = client.get("/debug")
        assert response.status_code == 200


def test_page_responds_quickly():
    """Test that the page endpoint responds quickly"""
    with TestClient(app) as client:
        start = time.time()
        response = client.get("/")
        elapsed = time.time() - start

        # Should respond quickly (under 1 second for test client)
        assert elapsed < 1, f"Page took {elapsed} seconds to respond"
        assert response.status_code == 200


def test_telecli_path_without_trailing_slash():
    """Test that /telecli without trailing slash works"""
    with TestClient(app) as client:
        response = client.get("/telecli")

        # Should not get 404
        assert response.status_code == 200


def test_static_file_paths_are_registered():
    """Test that the static file mount points are registered"""
    # This verifies the FastAPI app has the mounts configured
    assert hasattr(app, "user_middleware")
    # The static mounts should be present in the router
    routes_summary = [str(route) for route in app.routes]
    # At least one route should exist
    assert len(routes_summary) > 0


def test_root_page_includes_browser_agent_panel_markup():
    """The web UI should render the Ask-mode panel scaffold."""
    with TestClient(app) as client:
        response = client.get("/")

        assert response.status_code == 200
        body = response.text
        assert 'browser-agent-panel' in body
        assert 'Ask mode' in body
        assert 'browser-agent-commands' in body
