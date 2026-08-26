"""Tests for shared service wiring across entrypoints."""

import pytest

from src import web_app


class FakeSessionManager:
    def __init__(self):
        self.monitor_callback = None
        self.close_all_calls = 0

    def set_monitor_callback(self, callback):
        self.monitor_callback = callback

    async def close_all(self):
        self.close_all_calls += 1


@pytest.mark.asyncio
async def test_web_app_lifespan_preserves_injected_session_manager():
    """The web app should keep an injected shared session manager alive."""
    manager = FakeSessionManager()
    original_manager = web_app.session_manager
    original_managed = web_app._session_manager_managed

    try:
        web_app.set_session_manager(manager, managed=False)

        async with web_app.lifespan(web_app.app):
            assert web_app.session_manager is manager
            assert manager.monitor_callback is not None

        assert web_app.session_manager is manager
        assert manager.close_all_calls == 0
    finally:
        web_app.set_session_manager(original_manager, managed=original_managed)
