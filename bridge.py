#!/usr/bin/env python3
"""Kiro Slack Bridge - Connect Slack to Kiro CLI via ACP (Agent Client Protocol)"""

import asyncio
import os
import yaml
import logging
import time
from pathlib import Path
from datetime import datetime
from collections import defaultdict, deque
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.errors import SlackApiError

from acp_client import KiroACP, TurnResult
from session_store import SessionStore

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class Metrics:
    """Simple metrics collector"""

    def __init__(self):
        self.messages_processed = 0
        self.errors = defaultdict(int)
        self.kiro_execution_times = []
        self.start_time = time.time()

    def record_message(self):
        self.messages_processed += 1

    def record_error(self, error_type):
        self.errors[error_type] += 1

    def record_kiro_time(self, duration):
        self.kiro_execution_times.append(duration)
        if len(self.kiro_execution_times) > 100:
            self.kiro_execution_times.pop(0)

    def get_stats(self):
        uptime = time.time() - self.start_time
        avg_time = (
            sum(self.kiro_execution_times) / len(self.kiro_execution_times)
            if self.kiro_execution_times
            else 0
        )
        return {
            "uptime_seconds": int(uptime),
            "messages_processed": self.messages_processed,
            "errors": dict(self.errors),
            "avg_kiro_execution_time": round(avg_time, 2),
            "recent_executions": len(self.kiro_execution_times),
        }


class HealthHandler(BaseHTTPRequestHandler):
    """HTTP handler for health checks and metrics"""

    metrics = None

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"healthy"}')
        elif self.path == "/metrics":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            import json
            stats = self.metrics.get_stats() if self.metrics else {}
            self.wfile.write(json.dumps(stats).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


class KiroSlackBridge:
    def __init__(self, config_path="config.yaml"):
        with open(config_path) as f:
            config_raw = f.read()

        config_raw = os.path.expandvars(config_raw)
        self.config = yaml.safe_load(config_raw)

        self.app_token = self.config["slack"]["app_token"]
        self.bot_token = self.config["slack"]["bot_token"]

        if not self.app_token or not self.bot_token:
            raise ValueError(
                "Slack tokens not configured. Set SLACK_APP_TOKEN and SLACK_BOT_TOKEN environment variables."
            )

        self.base_dir = Path(self.config["threads"]["base_dir"]).expanduser()
        self.kiro_cli = self.config["kiro"].get("cli_path") or "kiro-cli"
        self.agent = self.config["kiro"].get("agent") or None

        # ACP config
        acp_config = self.config.get("acp", {})
        self.response_timeout = acp_config.get("response_timeout", 300)
        self.max_sessions = acp_config.get("max_sessions", 100)

        # Rate limiting
        rate_config = self.config.get("rate_limits", {})
        self.per_user_limit = rate_config.get("per_user_per_minute", 10)
        self.max_concurrent = rate_config.get("max_concurrent", 3)

        # Health check port
        health_config = self.config.get("health", {})
        self.health_port = health_config.get("port", 9090)

        self.user_messages = defaultdict(deque)
        self._semaphore = asyncio.Semaphore(self.max_concurrent)

        # Metrics
        self.metrics = Metrics()
        HealthHandler.metrics = self.metrics

        # Slack clients
        self.client = WebClient(token=self.bot_token)
        self.socket_client = SocketModeClient(
            app_token=self.app_token, web_client=self.client
        )

        # Session store
        store_path = self.base_dir / ".session_store.json"
        self.sessions = SessionStore(store_path, max_sessions=self.max_sessions)

        # ACP client (initialized lazily in the async loop)
        self._acp: KiroACP | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

        # Streaming state: track in-progress message edits
        self._streaming_messages: dict[str, dict] = {}

    def _get_acp(self) -> KiroACP:
        if self._acp is None:
            self._acp = KiroACP(
                cli_path=self.kiro_cli,
                agent=self.agent,
                on_chunk=self._on_chunk,
                on_tool_call=self._on_tool_call,
                response_timeout=self.response_timeout,
            )
        return self._acp

    def _on_chunk(self, session_id: str, chunk: str):
        """Called on each streaming chunk — schedule a Slack message edit."""
        state = self._streaming_messages.get(session_id)
        if state and self._loop:
            state["buffer"] += chunk
            # Throttle edits to ~1/sec
            now = time.time()
            if now - state.get("last_edit", 0) >= 1.0:
                state["last_edit"] = now
                text = state["buffer"]
                channel = state["channel"]
                ts = state["msg_ts"]
                try:
                    self.client.chat_update(channel=channel, ts=ts, text=text + " ⏳")
                except SlackApiError:
                    pass

    def _on_tool_call(self, session_id: str, tool_info: dict):
        """Called on tool call events — update status in Slack."""
        state = self._streaming_messages.get(session_id)
        if not state:
            return
        name = tool_info.get("name", "tool")
        status = tool_info.get("status", "")
        if status in ("pending", "running"):
            indicator = f"\n🔧 _{name}_..."
        elif status == "completed":
            indicator = f"\n✅ _{name}_ done"
        else:
            indicator = ""
        if indicator:
            state["tool_status"] = indicator

    def get_thread_dir(self, thread_ts: str) -> Path:
        """Get directory path for a thread based on timestamp."""
        dt = datetime.fromtimestamp(float(thread_ts))
        thread_dir = (
            self.base_dir
            / str(dt.year)
            / f"{dt.month:02d}"
            / f"{dt.day:02d}"
            / thread_ts
        )
        thread_dir.mkdir(parents=True, exist_ok=True)
        return thread_dir

    async def run_kiro(self, message: str, thread_ts: str) -> TurnResult:
        """Send message to Kiro via ACP, managing sessions per thread."""
        acp = self._get_acp()
        thread_dir = self.get_thread_dir(thread_ts)

        # Get or create session for this thread
        session_id = self.sessions.get(thread_ts)
        if not session_id:
            session_id = await acp.new_session(cwd=str(thread_dir))
            self.sessions.put(thread_ts, session_id, str(thread_dir))
        else:
            self.sessions.touch(thread_ts)

        start_time = time.time()
        try:
            result = await acp.prompt(session_id, message)
            self.metrics.record_kiro_time(time.time() - start_time)
            return result
        except Exception as e:
            logger.error(f"ACP prompt failed: {e}", exc_info=True)
            self.metrics.record_error("acp_error")
            return TurnResult(text="Sorry, I encountered an error processing your request.", stop_reason="error")

    def send_message(self, channel: str, thread_ts: str, text: str):
        """Send message to Slack with chunking for long responses."""
        MAX_LENGTH = 3000
        if len(text) <= MAX_LENGTH:
            try:
                self.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)
            except SlackApiError as e:
                logger.error(f"Failed to send message: {e.response['error']}")
        else:
            chunks = [text[i:i + MAX_LENGTH] for i in range(0, len(text), MAX_LENGTH)]
            for i, chunk in enumerate(chunks):
                try:
                    prefix = f"(Part {i+1}/{len(chunks)})\n" if len(chunks) > 1 else ""
                    self.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=prefix + chunk)
                except SlackApiError as e:
                    logger.error(f"Failed to send chunk {i+1}: {e.response['error']}")
                    break

    def check_rate_limit(self, user: str) -> bool:
        """Check if user is within rate limits."""
        now = time.time()
        minute_ago = now - 60
        while self.user_messages[user] and self.user_messages[user][0] < minute_ago:
            self.user_messages[user].popleft()
        if len(self.user_messages[user]) >= self.per_user_limit:
            return False
        self.user_messages[user].append(now)
        return True

    def handle_message(self, event: dict):
        """Handle incoming Slack message — dispatches to async loop."""
        if (
            event.get("bot_id")
            or event.get("subtype") == "bot_message"
            or event.get("app_id")
            or event.get("bot_profile")
            or not event.get("user")
        ):
            return

        text = event.get("text", "")
        channel = event["channel"]
        thread_ts = event.get("thread_ts") or event["ts"]
        user = event.get("user", "unknown")

        logger.info(f"Received message from {user} in thread {thread_ts}")

        if not self.check_rate_limit(user):
            logger.warning(f"Rate limit exceeded for user {user}")
            try:
                self.client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts,
                    text=f"⏱️ Rate limit hit ({self.per_user_limit}/min). Please wait.",
                )
            except SlackApiError:
                pass
            return

        # Schedule async work on the event loop
        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self._handle_message_async(text, channel, thread_ts, user), self._loop
            )

    async def _handle_message_async(self, text: str, channel: str, thread_ts: str, user: str):
        """Async message handler with streaming UX."""
        async with self._semaphore:
            try:
                # Post initial "thinking" message
                thinking = self.client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts, text="⏳ Thinking..."
                )
                msg_ts = thinking["ts"]

                # Get or create session_id for streaming state
                acp = self._get_acp()
                session_id = self.sessions.get(thread_ts)

                # Set up streaming state (keyed by thread for now, re-keyed after session creation)
                temp_key = f"pending_{thread_ts}"
                self._streaming_messages[temp_key] = {
                    "channel": channel,
                    "msg_ts": msg_ts,
                    "buffer": "",
                    "last_edit": 0,
                    "tool_status": "",
                }

                # Run the prompt
                thread_dir = self.get_thread_dir(thread_ts)

                # Session management - sessions stay active in the ACP process
                if session_id:
                    self.sessions.touch(thread_ts)
                else:
                    if not acp.is_running:
                        await acp.start()
                    session_id = await acp.new_session(cwd=str(thread_dir))
                    self.sessions.put(thread_ts, session_id, str(thread_dir))

                # Re-key streaming state with actual session_id
                self._streaming_messages[session_id] = self._streaming_messages.pop(temp_key)

                start_time = time.time()
                result = await acp.prompt(session_id, text)
                self.metrics.record_kiro_time(time.time() - start_time)

                # Clean up streaming state
                self._streaming_messages.pop(session_id, None)

                # Final message update
                response = result.text.strip() or "_(No response)_"
                try:
                    self.client.chat_update(channel=channel, ts=msg_ts, text=response)
                except SlackApiError:
                    # If update fails (e.g., too long), delete and re-post with chunking
                    try:
                        self.client.chat_delete(channel=channel, ts=msg_ts)
                    except SlackApiError:
                        pass
                    self.send_message(channel, thread_ts, response)

                # If response is too long for a single message, post overflow as chunks
                if len(response) > 3000:
                    try:
                        self.client.chat_delete(channel=channel, ts=msg_ts)
                    except SlackApiError:
                        pass
                    self.send_message(channel, thread_ts, response)

                self.metrics.record_message()
                logger.info(f"Sent response to {user} in thread {thread_ts}")

            except Exception as e:
                logger.error(f"Error handling message: {e}", exc_info=True)
                self.metrics.record_error("handler_error")
                # Clean up temp streaming state
                self._streaming_messages.pop(f"pending_{thread_ts}", None)
                try:
                    self.client.chat_postMessage(
                        channel=channel, thread_ts=thread_ts,
                        text="Sorry, I encountered an error processing your message.",
                    )
                except SlackApiError:
                    pass

    def process_event(self, client: SocketModeClient, req: SocketModeRequest):
        """Process Socket Mode events."""
        if req.type == "events_api":
            response = SocketModeResponse(envelope_id=req.envelope_id)
            client.send_socket_mode_response(response)
            event = req.payload["event"]
            if event["type"] in ["app_mention", "message"]:
                self.handle_message(event)

        elif req.type == "slash_commands":
            response = SocketModeResponse(envelope_id=req.envelope_id)
            client.send_socket_mode_response(response)
            self.handle_slash_command(req.payload)

    def handle_slash_command(self, payload: dict):
        """Handle slash commands."""
        command = payload["command"]
        channel = payload["channel_id"]
        user = payload["user_id"]
        logger.info(f"Received slash command {command} from {user}")

        try:
            if command == "/kiro-help":
                self.client.chat_postMessage(channel=channel, text=(
                    "🤖 *Kiro Slack Bridge (ACP)*\n\n"
                    "*Commands:*\n"
                    "- `/kiro-help` - Show this help\n"
                    "- `/kiro-reset` - Reset conversation in current thread\n\n"
                    "*Usage:*\n"
                    "- Mention @Kiro in a channel or DM directly\n"
                    "- Each thread maintains its own conversation (via ACP sessions)\n"
                    "- Responses stream in real-time"
                ))
            elif command == "/kiro-reset":
                # Find thread context from payload if available
                thread_ts = payload.get("thread_ts")
                if thread_ts:
                    self.sessions.remove(thread_ts)
                    self.client.chat_postMessage(channel=channel, text="🔄 Conversation reset. Next message starts fresh.")
                else:
                    self.client.chat_postMessage(channel=channel, text="Use `/kiro-reset` inside a thread to reset that conversation.")
            else:
                self.client.chat_postMessage(channel=channel, text=f"Unknown command `{command}`. Use `/kiro-help`.")
        except Exception as e:
            logger.error(f"Error handling slash command: {e}", exc_info=True)

    def start(self):
        """Start the bridge."""
        # Health check server
        health_server = HTTPServer(("0.0.0.0", self.health_port), HealthHandler)
        health_thread = Thread(target=health_server.serve_forever, daemon=True)
        health_thread.start()
        logger.info(f"🏥 Health check server on :{self.health_port}")

        # Async event loop in a background thread for ACP
        self._loop = asyncio.new_event_loop()
        loop_thread = Thread(target=self._loop.run_forever, daemon=True)
        loop_thread.start()

        # Start ACP process
        asyncio.run_coroutine_threadsafe(self._get_acp().start(), self._loop)

        # Slack socket mode
        self.socket_client.socket_mode_request_listeners.append(self.process_event)
        logger.info("🚀 Kiro Slack Bridge (ACP) starting...")
        logger.info(f"📁 Thread storage: {self.base_dir}")
        logger.info(f"⚡ Rate limit: {self.per_user_limit} msgs/min per user")
        logger.info(f"🔄 Max concurrent: {self.max_concurrent}")

        try:
            self.socket_client.connect()
            logger.info("✅ Connected to Slack")
            from threading import Event
            Event().wait()
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            if self._loop:
                asyncio.run_coroutine_threadsafe(self._get_acp().stop(), self._loop)
        except Exception as e:
            logger.error(f"Fatal error: {e}", exc_info=True)
            raise


if __name__ == "__main__":
    import sys
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    bridge = KiroSlackBridge(config_path)
    bridge.start()
