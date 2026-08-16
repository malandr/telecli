"""
FastAPI web application with WebSocket support
"""
import logging
import json
import asyncio
from datetime import datetime
from fastapi import FastAPI, WebSocket, HTTPException, Request, APIRouter
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from contextlib import asynccontextmanager
from starlette.websockets import WebSocketDisconnect
from pydantic import ValidationError
from src.session_manager import SessionManager
from src.config import Config
from src.ws_models import MAX_TERMINAL_DIMENSION, WebSocketMessage

logger = logging.getLogger(__name__)

# Global session manager
session_manager: SessionManager = None
_session_manager_managed = True  # lifespan handles lifecycle by default


def _parse_positive_int(raw_value: str | None) -> int | None:
    """Parse a websocket query parameter as a positive integer."""
    if raw_value is None:
        return None

    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return None

    if value <= 0:
        return None

    return min(value, MAX_TERMINAL_DIMENSION)


def set_session_manager(manager: SessionManager | None, *, managed: bool = False) -> None:
    """Inject a session manager instance for the web app lifecycle."""
    global session_manager, _session_manager_managed
    session_manager = manager
    _session_manager_managed = managed


async def send_json_locked(websocket: WebSocket, payload: dict, send_lock: asyncio.Lock) -> bool:
    """Serialize websocket JSON sends to avoid overlapping writes."""
    async with send_lock:
        await websocket.send_json(payload)
    return True


# Global LLM monitor data
llm_monitor_data = []
MAX_MONITOR_ENTRIES = 100

def add_llm_monitor_entry(entry_type: str, data: dict):
    """Add entry to LLM monitor data"""
    global llm_monitor_data
    entry = {
        "type": entry_type,
        "timestamp": datetime.now().isoformat(),
        "data": data
    }
    llm_monitor_data.append(entry)

    # Keep only recent entries
    if len(llm_monitor_data) > MAX_MONITOR_ENTRIES:
        llm_monitor_data = llm_monitor_data[-MAX_MONITOR_ENTRIES:]

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager"""
    global session_manager
    if _session_manager_managed:
        session_manager = SessionManager()
    session_manager.set_monitor_callback(add_llm_monitor_entry)

    logger.info("Web app started")
    yield
    if _session_manager_managed and session_manager is not None:
        await session_manager.close_all()
    logger.info("Web app stopped")


app = FastAPI(
    title="TeleCLI",
    lifespan=lifespan,
)

# Add middleware to handle reverse proxy headers
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])

# Router for all endpoints to support multiple prefixes
router = APIRouter()

@router.get("/")
async def get_root():
    """Serve the web UI"""
    return FileResponse("static/index.html")

@router.get("/style.css")
async def get_style():
    """Serve the CSS file"""
    return FileResponse("static/style.css")

@router.get("/debug")
async def debug_info(request: Request):
    """Debug endpoint to check request information"""
    return {
        "url": str(request.url),
        "method": request.method,
        "headers": dict(request.headers),
        "path": request.url.path,
        "query": request.url.query,
        "host": request.headers.get("host"),
        "x_forwarded_for": request.headers.get("x-forwarded-for"),
        "x_forwarded_proto": request.headers.get("x-forwarded-proto"),
    }

@router.get("/health")
async def health_check():
    """Health check endpoint"""
    stats = session_manager.get_stats()
    return {
        "status": "healthy",
        "sessions": stats,
    }

@router.get("/stats")
async def get_stats():
    """Get server statistics"""
    stats = session_manager.get_stats()
    return stats

@router.get("/api/sessions")
async def get_active_sessions():
    """Get TeleCLI session entries for the session picker."""
    return {"sessions": session_manager.list_sessions()}

@router.get("/api/tmux/sessions")
async def get_tmux_sessions():
    """List machine tmux sessions."""
    return {"sessions": session_manager.list_machine_tmux_sessions()}


@router.post("/api/tmux/sessions")
async def create_tmux_session(request: Request):
    """Create a new tmux session and import it into TeleCLI."""
    body = await request.json() if request.headers.get("content-length") else {}
    tmux_session_name = body.get("name", "").strip()
    if not tmux_session_name:
        raise HTTPException(status_code=400, detail="tmux session name is required")

    try:
        result = session_manager.create_tmux_session_entry(tmux_session_name)
        return {"session": result}
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/api/sessions")
async def create_session(request: Request):
    """Create a new TeleCLI session entry."""
    body = await request.json() if request.headers.get("content-length") else {}
    name = body.get("name", "").strip() or None
    result = session_manager.create_session_entry(name=name)
    return {"session": result}


@router.patch("/api/sessions/{session_id}")
async def patch_session(session_id: str, request: Request):
    """Rename a session."""
    body = await request.json()
    new_name = body.get("name", "").strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="name is required")
    try:
        result = session_manager.rename_session(session_id, new_name)
        return {"session": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    """Delete a session entry."""
    await session_manager.delete_session_entry(session_id)
    return {"status": "ok"}


@router.post("/api/sessions/{session_id}/detach")
async def detach_tmux_session(session_id: str):
    """Detach a tmux session from TeleCLI."""
    try:
        result = await session_manager.detach_tmux_session(session_id)
        return {"session": result}
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/sessions/import-tmux")
async def import_tmux_session(request: Request):
    """Import an existing machine tmux session into TeleCLI."""
    body = await request.json()
    tmux_session_name = body.get("tmux_session_name", "").strip()
    if not tmux_session_name:
        raise HTTPException(status_code=400, detail="tmux_session_name is required")
    name = body.get("name", "").strip() or None
    try:
        result = session_manager.import_tmux_session(tmux_session_name, name=name)
        return {"session": result}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/llm-monitor")
async def get_llm_monitor_data():
    """Get LLM monitor data"""
    return {"entries": llm_monitor_data}

@router.delete("/api/llm-monitor")
async def clear_llm_monitor_data():
    """Clear LLM monitor data"""
    global llm_monitor_data
    llm_monitor_data = []
    return {"status": "cleared"}

@router.get("/api/auth/required")
async def get_auth_required():
    """Get whether authentication is required"""
    return {
        "auth_required": Config.AUTH_REQUIRED
    }

@router.get("/api/ai-proxy/config")
async def get_ai_proxy_config():
    """Get AI proxy configuration (single source of truth)"""
    return {
        "default_provider": Config.AI_PROXY_PROVIDER,
        "default_system_prompt": Config.AI_PROXY_SYSTEM_PROMPT,
        "max_iterations": Config.AI_PROXY_MAX_ITERATIONS
    }

@router.post("/reset/{client_id}")
async def reset_session(client_id: str):
    """Reset a client's session"""
    try:
        await session_manager.close_session(client_id)
        logger.info(f"Reset session for client {client_id}")
        return {"status": "ok", "message": "Session reset"}
    except Exception as e:
        logger.error(f"Error resetting session {client_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    """WebSocket endpoint for bidirectional terminal streaming"""

    # Check authentication if required
    if Config.AUTH_REQUIRED:
        token = websocket.query_params.get("token")
        if not token or token != Config.AUTH_TOKEN:
            logger.warning(f"WebSocket auth failed for {client_id}: invalid or missing token")
            await websocket.close(code=1008, reason="Unauthorized")
            return

    await websocket.accept()
    logger.info(f"WebSocket connection established for client {client_id}")
    initial_rows = _parse_positive_int(websocket.query_params.get("rows"))
    initial_cols = _parse_positive_int(websocket.query_params.get("cols"))

    connection_active = True
    send_lock = asyncio.Lock()

    async def safe_send_json(payload: dict) -> bool:
        nonlocal connection_active
        if not connection_active:
            return False
        try:
            return await send_json_locked(websocket, payload, send_lock)
        except Exception:
            connection_active = False
            return False

    # Monitoring callback to send LLM info live to the UI
    async def llm_monitor_callback(entry_type: str, data: dict):
        await safe_send_json({
            "llm_monitor": {
                "type": entry_type,
                "data": data
            }
        })

    async def claude_code_status_callback(status: dict):
        await safe_send_json({"claude_code_status": status})

    async def browser_agent_status_callback(status: dict):
        await safe_send_json({"browser_agent": status})

    # Hook into AI proxy if it exists
    ai_proxy = session_manager.get_ai_proxy(client_id)
    if ai_proxy:
        ai_proxy.set_monitor_callback(llm_monitor_callback)
        if not await safe_send_json({"proxy_status": ai_proxy.get_status()}):
            return
    else:
        if not await safe_send_json({"proxy_status": {"enabled": False}}):
            return

    claude_code_auto = session_manager.get_claude_code_auto_continue(client_id)
    if claude_code_auto:
        claude_code_auto.set_status_callback(claude_code_status_callback)
        if not await safe_send_json({"claude_code_status": claude_code_auto.get_status()}):
            return
    else:
        if not await safe_send_json({"claude_code_status": {"enabled": False}}):
            return

    # Re-bind the browser-agent status callback if a run/approvals persisted from an earlier connection
    existing_browser_agent = session_manager.get_browser_agent(client_id)
    if existing_browser_agent:
        existing_browser_agent.set_status_callback(browser_agent_status_callback)
    if not await safe_send_json({"browser_agent": session_manager.get_browser_agent_status(client_id)}):
        return

    try:
        capabilities = session_manager.get_session_mode_capabilities(client_id)
    except Exception:
        logger.warning("Failed to get session mode capabilities for client %s", client_id, exc_info=True)
        capabilities = None

    if capabilities and capabilities.get("backend") == "tmux":
        try:
            current_screen = await asyncio.to_thread(
                session_manager.capture_session_screen,
                client_id,
            )
        except Exception as exc:
            logger.debug("Failed to capture tmux screen for %s during connect: %s", client_id, exc)
        else:
            if current_screen and not await safe_send_json({"output": current_screen}):
                return
    elif client_id in session_manager.sessions:
        # Non-tmux backend has no direct screen-capture — nudge a redraw instead.
        try:
            session = await session_manager.get_session(client_id)
            await asyncio.sleep(0.2)
            await session.send_input(" \b", newline=False)
            await asyncio.sleep(0.1)
            await session.send_input("", newline=True)
            await asyncio.sleep(0.1)
            await session.send_input("\x0C", newline=False)
        except Exception as e:
            logger.warning(f"Could not refresh terminal for {client_id}: {e}")

    async def handle_input():
        nonlocal connection_active
        try:
            while connection_active:
                data = await websocket.receive_text()
                try:
                    raw_message = json.loads(data)
                except json.JSONDecodeError:
                    continue

                try:
                    message = WebSocketMessage.model_validate(raw_message)
                except ValidationError:
                    continue

                if message.input:
                    await session_manager.send_input(client_id, message.input, newline=False, from_ai=False)
                    # Notify proxy of user interaction
                    ai_proxy = session_manager.get_ai_proxy(client_id)
                    if ai_proxy and ai_proxy.is_enabled():
                        ai_proxy.notify_user_input(message.input)

                if message.resize:
                    await session_manager.resize_session(
                        client_id,
                        message.resize.rows,
                        message.resize.cols,
                    )

                if message.proxy:
                    proxy_cmd = message.proxy
                    if proxy_cmd.enable:
                        success = await session_manager.enable_ai_proxy(
                            client_id,
                            proxy_cmd.provider,
                            proxy_cmd.system_prompt,
                        )
                        if success:
                            ai_proxy = session_manager.get_ai_proxy(client_id)
                            if ai_proxy:
                                ai_proxy.set_monitor_callback(llm_monitor_callback)
                                await safe_send_json({"proxy_status": ai_proxy.get_status()})
                    elif proxy_cmd.disable:
                        await session_manager.disable_ai_proxy(client_id)
                        await safe_send_json({"proxy_status": {"enabled": False}})

                if message.claude_code:
                    claude_code_cmd = message.claude_code
                    if claude_code_cmd.enable:
                        success = await session_manager.enable_claude_code_auto_continue(client_id)
                        if success:
                            claude_code_auto = session_manager.get_claude_code_auto_continue(client_id)
                            if claude_code_auto:
                                claude_code_auto.set_status_callback(claude_code_status_callback)
                                await safe_send_json({"claude_code_status": claude_code_auto.get_status()})
                    elif claude_code_cmd.disable:
                        await session_manager.disable_claude_code_auto_continue(client_id)
                        await safe_send_json({"claude_code_status": {"enabled": False}})
                    elif claude_code_cmd.screen_text:
                        claude_code_auto = session_manager.get_claude_code_auto_continue(client_id)
                        if claude_code_auto and claude_code_auto.is_enabled():
                            claude_code_auto.inspect_screen_text(claude_code_cmd.screen_text)

                if message.browser_agent:
                    browser_agent_cmd = message.browser_agent
                    if browser_agent_cmd.submit:
                        submit = browser_agent_cmd.submit
                        try:
                            await session_manager.start_browser_agent_run(
                                client_id,
                                prompt=submit.prompt,
                                provider_name=submit.provider,
                                state_callback=browser_agent_status_callback,
                            )
                        except Exception as exc:
                            logger.warning("Browser agent submit failed for %s: %s", client_id, exc)
                            status = session_manager.get_browser_agent_status(client_id)
                            status["error"] = str(exc)
                            await safe_send_json({"browser_agent": status})
                    elif browser_agent_cmd.approve:
                        approve = browser_agent_cmd.approve
                        try:
                            await session_manager.approve_browser_agent_run(
                                client_id,
                                target=approve.target,
                                scope=approve.scope,
                                command_ids=approve.command_ids,
                                pattern=approve.pattern,
                            )
                        except Exception as exc:
                            logger.warning("Browser agent approve failed for %s: %s", client_id, exc)
                    elif browser_agent_cmd.stop:
                        await session_manager.stop_browser_agent_run(client_id)
        except WebSocketDisconnect:
            connection_active = False
        except Exception as e:
            logger.error(f"Input handler error for {client_id}: {e}")
            connection_active = False

    async def handle_output():
        nonlocal connection_active
        try:
            async for chunk in session_manager.get_output_stream(
                client_id,
                rows=initial_rows,
                cols=initial_cols,
            ):
                if not connection_active:
                    break
                if chunk:
                    # Feed chunk to AI proxy for pattern detection
                    ai_proxy = session_manager.get_ai_proxy(client_id)
                    if ai_proxy and ai_proxy.is_enabled():
                        ai_proxy.add_output(chunk)

                    claude_code_auto = session_manager.get_claude_code_auto_continue(client_id)
                    if claude_code_auto and claude_code_auto.is_enabled():
                        claude_code_auto.add_output(chunk)

                    # Forward to browser
                    await safe_send_json({"output": chunk})
        except Exception:
            connection_active = False

    async def ai_proxy_checker():
        """Periodic check for prompt detection while AI proxy is enabled"""
        try:
            while connection_active:
                await asyncio.sleep(0.5)
                ai_proxy = session_manager.get_ai_proxy(client_id)
                if ai_proxy and ai_proxy.is_enabled():
                    await ai_proxy.process_output()
        except Exception:
            pass

    try:
        await asyncio.gather(
            handle_input(),
            handle_output(),
            ai_proxy_checker(),
            return_exceptions=True
        )
    finally:
        connection_active = False
        ai_proxy = session_manager.get_ai_proxy(client_id)
        if ai_proxy:
            ai_proxy.set_monitor_callback(None)
        claude_code_auto = session_manager.get_claude_code_auto_continue(client_id)
        if claude_code_auto:
            claude_code_auto.set_status_callback(None)
        browser_agent = session_manager.get_browser_agent(client_id)
        if browser_agent:
            browser_agent.set_status_callback(None)
        # Session is NOT closed here to allow persistence & reconnection
        # sessions are managed by max_sessions policy in SessionManager
        logger.info(f"WebSocket disconnected for {client_id}, session kept alive")


# Explicit route for /telecli without trailing slash to satisfy browsers/proxies
@app.get("/telecli")
async def get_telecli_root_no_slash():
    """Serve the web UI for /telecli without trailing slash"""
    return FileResponse("static/index.html")

# Include router at both root and /telecli prefix for flexible access
app.include_router(router)
app.include_router(router, prefix="/telecli")

# Mount static files (at both locations to be safe)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/telecli/static", StaticFiles(directory="static"), name="static_telecli")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host=Config.WEB_HOST,
        port=Config.WEB_PORT,
        access_log=True,
        server_header=False,
        date_header=False,
        forwarded_allow_ips="*",
        proxy_headers=True,
    )
