"""ACP client for communicating with kiro-cli acp over stdio JSON-RPC."""

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from acp import PROTOCOL_VERSION, spawn_agent_process, text_block
from acp.interfaces import Client

logger = logging.getLogger(__name__)


@dataclass
class TurnResult:
    """Accumulated result of a single prompt turn."""

    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = ""


class KiroACPClient(Client):
    """ACP client that bridges Slack messages to kiro-cli acp."""

    def __init__(self, on_chunk: Callable[[str, str], None] | None = None,
                 on_tool_call: Callable[[str, dict], None] | None = None):
        self._on_chunk = on_chunk
        self._on_tool_call = on_tool_call
        self._turn_results: dict[str, TurnResult] = {}
        self._turn_events: dict[str, asyncio.Event] = {}

    def _get_turn(self, session_id: str) -> TurnResult:
        if session_id not in self._turn_results:
            self._turn_results[session_id] = TurnResult()
        return self._turn_results[session_id]

    def _get_event(self, session_id: str) -> asyncio.Event:
        if session_id not in self._turn_events:
            self._turn_events[session_id] = asyncio.Event()
        return self._turn_events[session_id]

    async def request_permission(self, options, session_id, tool_call, **kwargs: Any):
        """Auto-approve all tool calls (trust-all equivalent)."""
        return {"outcome": {"outcome": "approved"}}

    async def session_update(self, session_id: str, update: Any, **kwargs):
        """Handle streaming session updates from the agent."""
        turn = self._get_turn(session_id)
        update_type = getattr(update, "type", None) or (
            update.get("type") if isinstance(update, dict) else None
        )

        if update_type == "AgentMessageChunk":
            content = getattr(update, "content", None) or (
                update.get("content") if isinstance(update, dict) else None
            )
            if content:
                chunk_text = ""
                if isinstance(content, str):
                    chunk_text = content
                elif isinstance(content, list):
                    for block in content:
                        t = getattr(block, "text", None) or (
                            block.get("text") if isinstance(block, dict) else None
                        )
                        if t:
                            chunk_text += t
                elif hasattr(content, "text"):
                    chunk_text = content.text
                turn.text += chunk_text
                if self._on_chunk:
                    self._on_chunk(session_id, chunk_text)

        elif update_type == "ToolCall":
            tool_info = {
                "name": getattr(update, "name", None) or (
                    update.get("name") if isinstance(update, dict) else "unknown"
                ),
                "status": getattr(update, "status", None) or (
                    update.get("status") if isinstance(update, dict) else "pending"
                ),
            }
            turn.tool_calls.append(tool_info)
            if self._on_tool_call:
                self._on_tool_call(session_id, tool_info)

        elif update_type == "ToolCallUpdate":
            tool_info = {
                "name": getattr(update, "name", None) or (
                    update.get("name") if isinstance(update, dict) else "unknown"
                ),
                "status": getattr(update, "status", None) or (
                    update.get("status") if isinstance(update, dict) else "running"
                ),
            }
            if self._on_tool_call:
                self._on_tool_call(session_id, tool_info)

        elif update_type == "TurnEnd":
            event = self._get_event(session_id)
            event.set()


class KiroACP:
    """Manages the kiro-cli acp process and sessions."""

    def __init__(self, cli_path: str = "kiro-cli", agent: str | None = None,
                 on_chunk: Callable[[str, str], None] | None = None,
                 on_tool_call: Callable[[str, dict], None] | None = None,
                 response_timeout: float = 300):
        self._cli_path = cli_path
        self._agent = agent
        self._on_chunk = on_chunk
        self._on_tool_call = on_tool_call
        self._response_timeout = response_timeout
        self._client: KiroACPClient | None = None
        self._conn = None
        self._proc = None
        self._ctx = None
        self._initialized = False

    async def start(self):
        """Spawn kiro-cli acp and initialize the connection."""
        if self._initialized:
            return

        args = ["acp"]
        if self._agent:
            args.extend(["--agent", self._agent])

        self._client = KiroACPClient(
            on_chunk=self._on_chunk,
            on_tool_call=self._on_tool_call,
        )

        self._ctx = spawn_agent_process(self._client, self._cli_path, *args)
        self._conn, self._proc = await self._ctx.__aenter__()
        await self._conn.initialize(protocol_version=PROTOCOL_VERSION)
        self._initialized = True
        logger.info("ACP connection initialized with kiro-cli")

    async def stop(self):
        """Shut down the ACP process."""
        if self._ctx:
            try:
                await self._ctx.__aexit__(None, None, None)
            except Exception:
                pass
        self._initialized = False
        self._conn = None
        self._proc = None
        logger.info("ACP connection closed")

    async def new_session(self, cwd: str) -> str:
        """Create a new ACP session. Returns session_id."""
        if not self._initialized:
            await self.start()
        result = await self._conn.new_session(cwd=cwd, mcp_servers=[])
        session_id = result.session_id
        logger.info(f"Created new ACP session: {session_id}")
        return session_id

    async def load_session(self, session_id: str) -> str:
        """Load an existing ACP session. Returns session_id."""
        if not self._initialized:
            await self.start()
        result = await self._conn.load_session(session_id=session_id)
        logger.info(f"Loaded ACP session: {session_id}")
        return result.session_id

    async def prompt(self, session_id: str, message: str) -> TurnResult:
        """Send a prompt and wait for the turn to complete. Returns accumulated result."""
        if not self._initialized:
            await self.start()

        # Reset turn state
        self._client._turn_results[session_id] = TurnResult()
        event = self._client._get_event(session_id)
        event.clear()

        await self._conn.prompt(
            session_id=session_id,
            prompt=[text_block(message)],
            message_id=str(uuid4()),
        )

        # Wait for TurnEnd
        try:
            await asyncio.wait_for(event.wait(), timeout=self._response_timeout)
        except asyncio.TimeoutError:
            logger.error(f"Prompt timed out for session {session_id}")
            turn = self._client._get_turn(session_id)
            if not turn.text:
                turn.text = "Sorry, your request timed out."
            turn.stop_reason = "timeout"
            return turn

        turn = self._client._get_turn(session_id)
        turn.stop_reason = "end_turn"
        return turn

    async def cancel(self, session_id: str):
        """Cancel the current operation in a session."""
        if self._conn:
            await self._conn.cancel(session_id=session_id)

    @property
    def is_running(self) -> bool:
        return self._initialized and self._proc is not None
