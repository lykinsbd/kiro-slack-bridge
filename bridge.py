#!/usr/bin/env python3
"""Kiro Slack Bridge - Connect Slack to Kiro CLI"""

import os
import subprocess
import yaml
import logging
import time
import re
from pathlib import Path
from datetime import datetime
from collections import defaultdict, deque
from threading import Semaphore, Thread
from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.errors import SlackApiError
from http.server import HTTPServer, BaseHTTPRequestHandler

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def strip_ansi_codes(text):
    """Remove ANSI color codes from text"""
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)


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
        # Keep only last 100
        if len(self.kiro_execution_times) > 100:
            self.kiro_execution_times.pop(0)
    
    def get_stats(self):
        uptime = time.time() - self.start_time
        avg_time = sum(self.kiro_execution_times) / len(self.kiro_execution_times) if self.kiro_execution_times else 0
        
        return {
            "uptime_seconds": int(uptime),
            "messages_processed": self.messages_processed,
            "errors": dict(self.errors),
            "avg_kiro_execution_time": round(avg_time, 2),
            "recent_executions": len(self.kiro_execution_times)
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
        # Suppress default logging
        pass


class KiroSlackBridge:
    def __init__(self, config_path="config.yaml"):
        with open(config_path) as f:
            config_raw = f.read()
        
        # Expand environment variables
        config_raw = os.path.expandvars(config_raw)
        self.config = yaml.safe_load(config_raw)
        
        self.app_token = self.config["slack"]["app_token"]
        self.bot_token = self.config["slack"]["bot_token"]
        
        if not self.app_token or not self.bot_token:
            raise ValueError("Slack tokens not configured. Set SLACK_APP_TOKEN and SLACK_BOT_TOKEN environment variables.")
        
        self.base_dir = Path(self.config["threads"]["base_dir"]).expanduser()
        self.kiro_cli = self.config["kiro"].get("cli_path") or "kiro-cli"
        self.agent = self.config["kiro"].get("agent")
        self.trust_all = self.config["kiro"].get("trust_all_tools", False)
        
        # Rate limiting
        rate_config = self.config.get("rate_limits", {})
        self.per_user_limit = rate_config.get("per_user_per_minute", 10)
        self.max_concurrent = rate_config.get("max_concurrent", 3)
        
        # Health check port
        health_config = self.config.get("health", {})
        self.health_port = health_config.get("port", 9090)
        
        # Track user message timestamps for rate limiting
        self.user_messages = defaultdict(deque)
        
        # Semaphore for concurrent process limit
        self.process_semaphore = Semaphore(self.max_concurrent)
        
        # Metrics
        self.metrics = Metrics()
        HealthHandler.metrics = self.metrics
        
        self.client = WebClient(token=self.bot_token)
        self.socket_client = SocketModeClient(
            app_token=self.app_token,
            web_client=self.client
        )
    
    def get_thread_dir(self, thread_ts):
        """Get directory path for a thread based on timestamp"""
        dt = datetime.fromtimestamp(float(thread_ts))
        thread_dir = self.base_dir / str(dt.year) / f"{dt.month:02d}" / f"{dt.day:02d}" / thread_ts
        thread_dir.mkdir(parents=True, exist_ok=True)
        return thread_dir
    
    def has_existing_session(self, thread_dir):
        """Check if thread directory has an existing Kiro session"""
        try:
            result = subprocess.run(
                [self.kiro_cli, "chat", "--list-sessions"],
                cwd=thread_dir,
                capture_output=True,
                text=True,
                timeout=5
            )
            # If there's output with session info, a session exists
            return result.returncode == 0 and "Chat SessionId:" in result.stderr
        except Exception as e:
            logger.warning(f"Failed to check for existing session: {e}")
            return False
    
    def run_kiro(self, message, thread_dir, is_resume=False):
        """Run kiro-cli and return response"""
        cmd = [self.kiro_cli, "chat", "--no-interactive"]
        
        if is_resume:
            cmd.append("--resume")
        
        if self.agent:
            cmd.extend(["--agent", self.agent])
        
        if self.trust_all:
            cmd.append("--trust-all-tools")
        
        cmd.append(message)
        
        start_time = time.time()
        try:
            result = subprocess.run(
                cmd,
                cwd=thread_dir,
                capture_output=True,
                text=True,
                timeout=300  # 5 minute timeout
            )
            
            duration = time.time() - start_time
            self.metrics.record_kiro_time(duration)
            
            if result.returncode == 0:
                # Strip ANSI color codes from output
                return strip_ansi_codes(result.stdout.strip())
            else:
                logger.error(f"Kiro CLI failed: {result.stderr}")
                self.metrics.record_error("kiro_cli_error")
                return "Sorry, I encountered an error processing your request."
        
        except subprocess.TimeoutExpired:
            logger.error(f"Kiro CLI timeout for message: {message[:50]}...")
            self.metrics.record_error("timeout")
            return "Sorry, your request took too long to process (timeout after 5 minutes)."
        except Exception as e:
            logger.error(f"Unexpected error running Kiro CLI: {e}", exc_info=True)
            self.metrics.record_error("unexpected")
            return "Sorry, an unexpected error occurred."
    
    def send_message(self, channel, thread_ts, text):
        """Send message to Slack with chunking for long responses"""
        MAX_LENGTH = 3000
        
        if len(text) <= MAX_LENGTH:
            try:
                self.client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=text
                )
            except SlackApiError as e:
                logger.error(f"Failed to send message: {e.response['error']}")
        else:
            # Split into chunks
            chunks = [text[i:i+MAX_LENGTH] for i in range(0, len(text), MAX_LENGTH)]
            for i, chunk in enumerate(chunks):
                try:
                    prefix = f"(Part {i+1}/{len(chunks)})\n" if len(chunks) > 1 else ""
                    self.client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=prefix + chunk
                    )
                except SlackApiError as e:
                    logger.error(f"Failed to send chunk {i+1}: {e.response['error']}")
                    break
    
    def check_rate_limit(self, user):
        """Check if user is within rate limits"""
        now = time.time()
        minute_ago = now - 60
        
        # Remove old timestamps
        while self.user_messages[user] and self.user_messages[user][0] < minute_ago:
            self.user_messages[user].popleft()
        
        # Check limit
        if len(self.user_messages[user]) >= self.per_user_limit:
            return False
        
        # Add current timestamp
        self.user_messages[user].append(now)
        return True
    
    def handle_message(self, event):
        """Handle incoming Slack message"""
        # Ignore bot messages (including our own)
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            return
        
        text = event.get("text", "")
        channel = event["channel"]
        thread_ts = event.get("thread_ts") or event["ts"]
        user = event.get("user", "unknown")
        
        logger.info(f"Received message from {user} in thread {thread_ts}")
        
        # Check rate limit
        if not self.check_rate_limit(user):
            logger.warning(f"Rate limit exceeded for user {user}")
            try:
                self.client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=f"⏱️ Slow down! You've hit the rate limit ({self.per_user_limit} messages per minute). Please wait a moment."
                )
            except SlackApiError:
                pass
            return
        
        try:
            # Acquire semaphore for concurrency control
            if not self.process_semaphore.acquire(blocking=False):
                logger.warning(f"Max concurrent processes reached, queueing message from {user}")
                self.client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text="⏳ I'm currently handling other requests. Your message is queued..."
                )
                self.process_semaphore.acquire()  # Block until available
            
            try:
                # Get thread directory
                thread_dir = self.get_thread_dir(thread_ts)
                is_resume = self.has_existing_session(thread_dir)
                
                logger.debug(f"Thread dir: {thread_dir}, resume: {is_resume}")
                
                # Send typing indicator
                thinking_msg = self.client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text="Thinking..."
                )
                
                # Run Kiro
                response = self.run_kiro(text, thread_dir, is_resume)
                
                # Delete thinking message
                try:
                    self.client.chat_delete(
                        channel=channel,
                        ts=thinking_msg["ts"]
                    )
                except SlackApiError:
                    pass  # Ignore if we can't delete
                
                # Send response (with chunking)
                self.send_message(channel, thread_ts, response)
                
                self.metrics.record_message()
                logger.info(f"Sent response to {user} in thread {thread_ts}")
            finally:
                # Always release semaphore
                self.process_semaphore.release()
        
        except Exception as e:
            logger.error(f"Error handling message: {e}", exc_info=True)
            try:
                self.client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text="Sorry, I encountered an error processing your message."
                )
            except SlackApiError:
                pass
    
    def process_event(self, client: SocketModeClient, req: SocketModeRequest):
        """Process Socket Mode events"""
        if req.type == "events_api":
            # Acknowledge the request
            response = SocketModeResponse(envelope_id=req.envelope_id)
            client.send_socket_mode_response(response)
            
            event = req.payload["event"]
            
            # Handle app mentions and DMs
            if event["type"] in ["app_mention", "message"]:
                self.handle_message(event)
        
        elif req.type == "slash_commands":
            # Acknowledge the request
            response = SocketModeResponse(envelope_id=req.envelope_id)
            client.send_socket_mode_response(response)
            
            # Handle slash command
            self.handle_slash_command(req.payload)
    
    def handle_slash_command(self, payload):
        """Handle slash commands"""
        command = payload["command"]
        channel = payload["channel_id"]
        user = payload["user_id"]
        # Slash commands don't have thread_ts, use trigger_id or response_url
        thread_ts = None  # Slash commands respond ephemerally or to channel
        
        logger.info(f"Received slash command {command} from {user}")
        
        try:
            if command == "/kiro-help":
                help_text = """🤖 *Kiro Slack Bridge Help*

*Commands:*
• `/kiro-help` - Show this help message

*Usage:*
• Mention @Kiro Assistant in a channel or DM directly
• Each thread maintains its own conversation history
• Bot responds with full context from previous messages in the thread"""
                
                self.client.chat_postMessage(
                    channel=channel,
                    text=help_text
                )
            
            else:
                # Other commands not yet supported without thread context
                self.client.chat_postMessage(
                    channel=channel,
                    text=f"Command `{command}` is not yet implemented. Use `/kiro-help` for available commands."
                )
• Conversations are thread-based with full context
• Each thread maintains its own conversation history"""
                
                self.client.chat_postMessage(
                    channel=channel,
                    text=help_text
                )
        
        except Exception as e:
            logger.error(f"Error handling slash command: {e}", exc_info=True)
            try:
                self.client.chat_postMessage(
                    channel=channel,
                    text="Sorry, I encountered an error processing that command."
                )
            except SlackApiError:
                pass
    
    def start(self):
        """Start the bridge"""
        # Start health check server
        health_server = HTTPServer(("0.0.0.0", self.health_port), HealthHandler)
        health_thread = Thread(target=health_server.serve_forever, daemon=True)
        health_thread.start()
        logger.info(f"🏥 Health check server started on :{self.health_port}")
        
        self.socket_client.socket_mode_request_listeners.append(self.process_event)
        logger.info("🚀 Kiro Slack Bridge starting...")
        logger.info(f"📁 Thread storage: {self.base_dir}")
        logger.info(f"⚡ Rate limit: {self.per_user_limit} msgs/min per user")
        logger.info(f"🔄 Max concurrent: {self.max_concurrent} processes")
        
        try:
            self.socket_client.connect()
            logger.info("✅ Connected to Slack")
            
            from threading import Event
            Event().wait()
        except KeyboardInterrupt:
            logger.info("Shutting down...")
        except Exception as e:
            logger.error(f"Fatal error: {e}", exc_info=True)
            raise


if __name__ == "__main__":
    bridge = KiroSlackBridge()
    bridge.start()
