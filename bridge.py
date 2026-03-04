#!/usr/bin/env python3
"""Kiro Slack Bridge - Connect Slack to Kiro CLI"""

import os
import subprocess
import yaml
from pathlib import Path
from datetime import datetime
from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse


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
        
        result = subprocess.run(
            cmd,
            cwd=thread_dir,
            capture_output=True,
            text=True
        )
        
        return result.stdout.strip() if result.returncode == 0 else f"Error: {result.stderr}"
    
    def handle_message(self, event):
        """Handle incoming Slack message"""
        # Ignore bot messages
        if event.get("bot_id"):
            return
        
        text = event.get("text", "")
        channel = event["channel"]
        thread_ts = event.get("thread_ts") or event["ts"]
        
        # Check if this is a new thread or continuation
        thread_dir = self.get_thread_dir(thread_ts)
        is_resume = (thread_dir / ".kiro").exists() if hasattr(thread_dir / ".kiro", "exists") else False
        
        # Send typing indicator
        self.client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text="Thinking..."
        )
        
        # Run Kiro
        response = self.run_kiro(text, thread_dir, is_resume)
        
        # Send response
        self.client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=response
        )
    
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
        print(f"🚀 Kiro Slack Bridge starting...")
        print(f"📁 Thread storage: {self.base_dir}")
        self.socket_client.connect()
        print("✅ Connected to Slack")
        
        from threading import Event
        Event().wait()


if __name__ == "__main__":
    bridge = KiroSlackBridge()
    bridge.start()
