"""ACP client for communicating with kiro-cli acp over stdio JSON-RPC."""

import asyncio
import logging
from dataclasses import dataclass, field
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

    def _get_turn(self, session_id: str) -> TurnResult:
        if session_id not in self._turn_results:
            self._turn_results[session_id] = TurnResult()
        return self._turn_results[session_id]

    async def request_permission(self, options, session_id, tool_call, **kwargs: Any):
        """Auto-approve all tool calls (trust-all equivalent)."""
        logger.debug(f"Auto-approving tool call for session {session_id}")
        return {"outcome": {"outcome": "approved"}}

    async def session_update(self, session_id: str, update: Any, **kwargs):
        """Handle streaming session updates from the agent."""
        turn = self._get_turn(session_id)
        update_type = type(update).__name__

        if update_type == "AgentMessageChunk":
            content = getattr(update, "content", None)
            if content:
                chunk_text = ""
                if isinstance(content, str):
                    chunk_text = content
                elif isinstance(content, list):
                    for block in content:
                        t = getattr(block, "text", None)
                        if t:
                            chunk_text += t
                elif hasattr(content, "text"):
                    chunk_text = content.text
                if chunk_text:
                    turn.text += chunk_text
                    if self._on_chunk:
                        self._on_chunk(session_id, chunk_text)

        elif update_type == "ToolCallStart":
            tool_info = {
                "name": getattr(update, "name", "tool"),
                "status": "running",
            }
            turn.tool_calls.append(tool_info)
            logger.info(f"Tool call started: {tool_info['name']}")
            if self._on_tool_call:
                self._on_tool_call(session_id, tool_info)

        elif update_type == "ToolCallProgress":
            tool_info = {
                "name": getattr(update, "name", "tool"),
                "status": getattr(update, "status", "running"),
            }
            logger.debug(f"Tool call progress: {tool_info['name']} ({tool_info['status']})")
            if self._on_tool_call:
                self._on_tool_call(session_id, tool_info)

        else:
            logger.debug(f"Unhandled session update type: {update_type}")

    async def read_text_file(self, path: str, session_id: str, **kwargs: Any):
        """Handle file read requests from the agent."""
        logger.debug(f"Agent reading file: {path}")
        try:
            with open(path) as f:
                content = f.read()
            from acp.schema import ReadTextFileResponse
            return ReadTextFileResponse(content=content)
        except Exception as e:
            logger.warning(f"Failed to read file {path}: {e}")
            from acp.schema import ReadTextFileResponse
            return ReadTextFileResponse(content=f"Error reading file: {e}")

    async def write_text_file(self, content: str, path: str, session_id: str, **kwargs: Any):
        """Handle file write requests from the agent."""
        logger.info(f"Agent writing file: {path} ({len(content)} bytes)")
        try:
            with open(path, "w") as f:
                f.write(content)
            from acp.schema import WriteTextFileResponse
            return WriteTextFileResponse(success=True)
        except Exception as e:
            logger.warning(f"Failed to write file {path}: {e}")
            from acp.schema import WriteTextFileResponse
            return WriteTextFileResponse(success=False)


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

        logger.debug(f"Spawning: {self._cli_path} {' '.join(args)}")
        self._ctx = spawn_agent_process(self._client, self._cli_path, *args)
        self._conn, self._proc = await self._ctx.__aenter__()
        await self._conn.initialize(protocol_version=PROTOCOL_VERSION)
        self._initialized = True
        logger.info("ACP connection initialized with kiro-cli")

    async def stop(self):
        """Shut down the ACP process."""
        logger.debug("Stopping ACP process")
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
        logger.info(f"Created new ACP session: {session_id} (cwd={cwd})")
        return session_id

    async def load_session(self, session_id: str) -> str:
        """Load an existing ACP session. Returns session_id."""
        if not self._initialized:
            await self.start()
        result = await self._conn.load_session(session_id=session_id)
        logger.info(f"Loaded ACP session: {session_id}")
        return result.session_id

    async def prompt(self, session_id: str, message: str) -> TurnResult:
        """Send a prompt and wait for completion. Returns accumulated result."""
        if not self._initialized:
            await self.start()

        # Reset turn state
        self._client._turn_results[session_id] = TurnResult()
        logger.debug(f"Sending prompt to session {session_id}: {message[:100]}{'...' if len(message) > 100 else ''}")

        # prompt() blocks until the agent finishes the turn
        try:
            response = await asyncio.wait_for(
                self._conn.prompt(
                    session_id=session_id,
                    prompt=[text_block(message)],
                    message_id=str(uuid4()),
                ),
                timeout=self._response_timeout,
            )
        except asyncio.TimeoutError:
            logger.error(f"Prompt timed out after {self._response_timeout}s for session {session_id}")
            turn = self._client._get_turn(session_id)
            if not turn.text:
                turn.text = "Sorry, your request timed out."
            turn.stop_reason = "timeout"
            return turn

        turn = self._client._get_turn(session_id)
        turn.stop_reason = getattr(response, "stop_reason", "end_turn")
        logger.debug(f"Turn complete: {len(turn.text)} chars, {len(turn.tool_calls)} tool calls, reason={turn.stop_reason}")
        return turn

    async def cancel(self, session_id: str):
        """Cancel the current operation in a session."""
        if self._conn:
            logger.info(f"Cancelling session {session_id}")
            await self._conn.cancel(session_id=session_id)

    @property
    def is_running(self) -> bool:
        return self._initialized and self._proc is not None
