#!/usr/bin/env python3
"""Kiro Slack Bridge - Connect Slack to Kiro CLI"""

import os
import subprocess
import yaml
import logging
from pathlib import Path
from datetime import datetime
from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.errors import SlackApiError

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


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
        
        try:
            result = subprocess.run(
                cmd,
                cwd=thread_dir,
                capture_output=True,
                text=True,
                timeout=300  # 5 minute timeout
            )
            
            if result.returncode == 0:
                return result.stdout.strip()
            else:
                logger.error(f"Kiro CLI failed: {result.stderr}")
                return "Sorry, I encountered an error processing your request."
        
        except subprocess.TimeoutExpired:
            logger.error(f"Kiro CLI timeout for message: {message[:50]}...")
            return "Sorry, your request took too long to process (timeout after 5 minutes)."
        except Exception as e:
            logger.error(f"Unexpected error running Kiro CLI: {e}", exc_info=True)
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
    
    def handle_message(self, event):
        """Handle incoming Slack message"""
        # Ignore bot messages
        if event.get("bot_id"):
            return
        
        text = event.get("text", "")
        channel = event["channel"]
        thread_ts = event.get("thread_ts") or event["ts"]
        user = event.get("user", "unknown")
        
        logger.info(f"Received message from {user} in thread {thread_ts}")
        
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
            
            logger.info(f"Sent response to {user} in thread {thread_ts}")
        
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
    
    def start(self):
        """Start the bridge"""
        self.socket_client.socket_mode_request_listeners.append(self.process_event)
        logger.info("🚀 Kiro Slack Bridge starting...")
        logger.info(f"📁 Thread storage: {self.base_dir}")
        
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
