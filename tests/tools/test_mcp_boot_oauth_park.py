"""Boot-time OAuth failures must park (timed self-probe), not kill the task.

Two production defects observed on the jd-ai-assistant gateway:

1. The MCP SDK raises ``OAuthNonInteractiveError`` inside its anyio task
   group, so it reaches Hermes wrapped in an ``ExceptionGroup``.
   ``_is_auth_error`` used plain ``isinstance`` and never unwrapped the
   group, misclassifying real auth failures.

2. ``MCPServerTask.run()``'s first-connect auth branch bare-``return``ed:
   the server task died permanently and only a full gateway restart could
   bring the server back, even after credentials were refreshed. It must
   park and self-probe like the reconnect and initial-connect paths.
"""

import asyncio

import pytest


pytest.importorskip("mcp.client.auth.oauth2")


def test_is_auth_error_unwraps_exception_group():
    from tools.mcp_tool import _is_auth_error
    from tools.mcp_oauth import OAuthNonInteractiveError

    wrapped = ExceptionGroup(
        "unhandled errors in a TaskGroup (1 sub-exception)",
        [OAuthNonInteractiveError("no browser")],
    )
    assert _is_auth_error(wrapped) is True


def test_is_auth_error_unwraps_nested_exception_group():
    from tools.mcp_tool import _is_auth_error
    from tools.mcp_oauth import OAuthNonInteractiveError

    nested = ExceptionGroup(
        "outer",
        [ExceptionGroup("inner", [OAuthNonInteractiveError("no browser")])],
    )
    assert _is_auth_error(nested) is True


def test_is_auth_error_rejects_non_auth_exception_group():
    from tools.mcp_tool import _is_auth_error

    group = ExceptionGroup("outer", [RuntimeError("boom"), ValueError("nope")])
    assert _is_auth_error(group) is False


@pytest.mark.no_isolate
def test_boot_oauth_failure_parks_and_self_probes(monkeypatch, tmp_path):
    """A server whose FIRST connect fails with an OAuth error must park with
    a timed self-probe and revive once auth succeeds — without any gateway
    restart or explicit _reconnect_event.set()."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool
    from tools.mcp_tool import MCPServerTask
    from tools.mcp_oauth import OAuthNonInteractiveError

    monkeypatch.setattr(mcp_tool, "_PARKED_RETRY_INTERVAL", 0.05)

    _real_sleep = asyncio.sleep

    async def _fast_sleep(_delay, *a, **kw):
        await _real_sleep(0)

    monkeypatch.setattr(mcp_tool.asyncio, "sleep", _fast_sleep)

    state = {
        "transport_calls": 0,
        "deregistered": 0,
        "auth_ok": False,
        "registrations": 0,
    }

    async def _scenario():
        class _Task(MCPServerTask):
            def _is_http(self):
                return False

            def _deregister_tools(self):
                state["deregistered"] += 1
                self._registered_tool_names = []

            def _register_discovered_tools_if_needed(self):
                if self._ready.is_set() and not self._registered_tool_names:
                    state["registrations"] += 1
                    self._registered_tool_names = ["srv__tool"]

            async def _run_stdio(self, config):
                state["transport_calls"] += 1
                if not state["auth_ok"]:
                    # Boot-time OAuth failure: no session was ever
                    # established, _ready is not set yet.
                    raise OAuthNonInteractiveError(
                        "MCP OAuth requires browser authorization but no "
                        "interactive session is available"
                    )
                self.session = object()
                self._ready.set()
                self._register_discovered_tools_if_needed()
                await self._wait_for_lifecycle_event()

        task = _Task("srv")

        run_task = asyncio.ensure_future(task.run({"command": "x"}))

        # The auth failure must PARK the task, not end it.
        for _ in range(2000):
            await _real_sleep(0)
            if state["deregistered"] >= 1:
                break
        assert not run_task.done(), (
            "run task exited on boot OAuth failure instead of parking "
            f"(transport_calls={state['transport_calls']})"
        )
        assert state["deregistered"] >= 1, "server never parked"

        # Credentials refreshed externally (e.g. `hermes mcp login`).
        # Revival must come from the timed self-probe alone.
        state["auth_ok"] = True
        for _ in range(200):
            await _real_sleep(0.01)
            if task.session is not None:
                break

        assert task.session is not None, (
            "parked server never self-probed back to life after auth "
            f"recovery (transport_calls={state['transport_calls']})"
        )
        assert state["registrations"] >= 1, (
            "revived server did not register its tools"
        )

        task._shutdown_event.set()
        task._reconnect_event.set()
        try:
            await asyncio.wait_for(run_task, timeout=2)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            run_task.cancel()

    asyncio.run(_scenario())
