"""Tests for InProcessTransport edge cases."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.a2a.transport import InProcessTransport


class TestInProcessTransport:
    def _make_transport(self, handler=None):
        registry = AsyncMock()
        registry._get_handler_for_transport = AsyncMock(return_value=handler)
        transport = InProcessTransport(registry=registry)
        return transport, registry

    @pytest.mark.asyncio
    async def test_send_120s_timeout(self):
        """G16: InProcessTransport.send must enforce a timeout."""

        class _SlowHandler:
            async def handle_task(self, _task):
                await asyncio.sleep(10)

        transport, _registry = self._make_transport(handler=_SlowHandler())

        from app.models.agent import AgentTask

        # Patch the default timeout to a very short value for the test
        original_timeout = InProcessTransport._DEFAULT_TIMEOUT
        InProcessTransport._DEFAULT_TIMEOUT = 0.01
        try:
            task = AgentTask(description="test", user_text="test")
            with pytest.raises(TimeoutError):
                await transport.send("light-agent", task, "req-1")
        finally:
            InProcessTransport._DEFAULT_TIMEOUT = original_timeout

    @pytest.mark.asyncio
    async def test_send_exception_wrapping_preserves_cause(self):
        """G16: InProcessTransport.send must wrap exceptions with `from e` preserving the cause."""
        original_error = ValueError("something broke")

        class _FailingHandler:
            async def handle_task(self, _task):
                raise original_error

        transport, _registry = self._make_transport(handler=_FailingHandler())
        from app.models.agent import AgentTask

        task = AgentTask(description="test", user_text="test")
        with pytest.raises(RuntimeError) as exc_info:
            await transport.send("light-agent", task, "req-1")

        assert "Agent error: light-agent" in str(exc_info.value)
        assert exc_info.value.__cause__ is original_error

    @pytest.mark.asyncio
    async def test_send_agent_not_found(self):
        """G16: InProcessTransport.send must raise RuntimeError when agent not found."""
        transport, _registry = self._make_transport(handler=None)
        from app.models.agent import AgentTask

        task = AgentTask(description="test", user_text="test")
        with pytest.raises(RuntimeError) as exc_info:
            await transport.send("missing-agent", task, "req-1")

        assert "Agent not found: missing-agent" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_stream_agent_not_found_yields_error(self):
        """G16: InProcessTransport.stream must yield an error chunk when agent not found."""
        transport, _registry = self._make_transport(handler=None)
        from app.models.agent import AgentTask

        task = AgentTask(description="test", user_text="test")
        chunks = [c async for c in transport.stream("missing-agent", task, "req-1")]
        assert len(chunks) == 1
        assert chunks[0]["done"] is True
        assert "Agent not found: missing-agent" in chunks[0].get("error", "")
