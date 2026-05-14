"""Tests for ACP-based Kiro Slack Bridge."""

import asyncio
import json
import time
import pytest
from pathlib import Path
from unittest.mock import Mock, patch, AsyncMock, MagicMock

from session_store import SessionStore
from acp_client import KiroACPClient, KiroACP, TurnResult


# === SessionStore Tests ===


class TestSessionStore:
    def test_put_and_get(self, tmp_path):
        store = SessionStore(tmp_path / "sessions.json")
        store.put("1234.5678", "sess_abc", "/tmp/dir")
        assert store.get("1234.5678") == "sess_abc"

    def test_get_missing_returns_none(self, tmp_path):
        store = SessionStore(tmp_path / "sessions.json")
        assert store.get("nonexistent") is None

    def test_persistence(self, tmp_path):
        path = tmp_path / "sessions.json"
        store = SessionStore(path)
        store.put("1234.5678", "sess_abc", "/tmp/dir")

        # Load fresh instance
        store2 = SessionStore(path)
        assert store2.get("1234.5678") == "sess_abc"

    def test_lru_eviction(self, tmp_path):
        store = SessionStore(tmp_path / "sessions.json", max_sessions=2)
        store.put("ts1", "sess1", "/dir1")
        store.put("ts2", "sess2", "/dir2")
        store.put("ts3", "sess3", "/dir3")  # evicts ts1

        assert store.get("ts1") is None
        assert store.get("ts2") == "sess2"
        assert store.get("ts3") == "sess3"

    def test_touch_moves_to_end(self, tmp_path):
        store = SessionStore(tmp_path / "sessions.json", max_sessions=2)
        store.put("ts1", "sess1", "/dir1")
        store.put("ts2", "sess2", "/dir2")
        store.touch("ts1")  # ts1 is now most recent
        store.put("ts3", "sess3", "/dir3")  # evicts ts2

        assert store.get("ts1") == "sess1"
        assert store.get("ts2") is None

    def test_remove(self, tmp_path):
        store = SessionStore(tmp_path / "sessions.json")
        store.put("ts1", "sess1", "/dir1")
        store.remove("ts1")
        assert store.get("ts1") is None

    def test_len(self, tmp_path):
        store = SessionStore(tmp_path / "sessions.json")
        assert len(store) == 0
        store.put("ts1", "sess1", "/dir1")
        assert len(store) == 1

    def test_corrupt_file_handled(self, tmp_path):
        path = tmp_path / "sessions.json"
        path.write_text("not valid json{{{")
        store = SessionStore(path)
        assert len(store) == 0


# === KiroACPClient Tests ===


class TestKiroACPClient:
    def test_request_permission_auto_approves(self):
        client = KiroACPClient()
        result = asyncio.run(client.request_permission(None, "sess1", None))
        assert result == {"outcome": {"outcome": "approved"}}

    def test_on_chunk_callback(self):
        chunks = []
        client = KiroACPClient(on_chunk=lambda sid, c: chunks.append((sid, c)))

        update = Mock()
        update.type = "AgentMessageChunk"
        update.content = [Mock(text="hello "), Mock(text="world")]

        asyncio.run(client.session_update("sess1", update))
        assert chunks == [("sess1", "hello world")]
        assert client._get_turn("sess1").text == "hello world"

    def test_tool_call_callback(self):
        calls = []
        client = KiroACPClient(on_tool_call=lambda sid, info: calls.append(info))

        update = Mock()
        update.type = "ToolCall"
        update.name = "read_file"
        update.status = "pending"

        asyncio.run(client.session_update("sess1", update))
        assert calls == [{"name": "read_file", "status": "pending"}]

    def test_turn_end_sets_event(self):
        client = KiroACPClient()
        event = client._get_event("sess1")
        assert not event.is_set()

        update = Mock()
        update.type = "TurnEnd"
        asyncio.run(client.session_update("sess1", update))
        assert event.is_set()

    def test_string_content_chunk(self):
        client = KiroACPClient()
        update = Mock()
        update.type = "AgentMessageChunk"
        update.content = "direct string"

        asyncio.run(client.session_update("sess1", update))
        assert client._get_turn("sess1").text == "direct string"


# === Bridge Config Tests ===


class TestBridgeConfig:
    def test_config_loading(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text("""
slack:
  app_token: "xapp-test"
  bot_token: "xoxb-test"
threads:
  base_dir: "{}"
kiro:
  cli_path: "kiro-cli"
  agent: ""
acp:
  response_timeout: 120
  max_sessions: 50
rate_limits:
  per_user_per_minute: 5
  max_concurrent: 2
health:
  port: 8080
""".format(tmp_path / "threads"))

        with patch("bridge.WebClient"), patch("bridge.SocketModeClient"):
            from bridge import KiroSlackBridge
            bridge = KiroSlackBridge(str(config_path))
            assert bridge.app_token == "xapp-test"
            assert bridge.response_timeout == 120
            assert bridge.max_sessions == 50
            assert bridge.per_user_limit == 5
            assert bridge.health_port == 8080

    def test_missing_tokens_raises(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text("""
slack:
  app_token: ""
  bot_token: ""
threads:
  base_dir: "~/threads"
kiro:
  cli_path: ""
  agent: ""
""")
        with patch("bridge.WebClient"), patch("bridge.SocketModeClient"):
            from bridge import KiroSlackBridge
            with pytest.raises(ValueError):
                KiroSlackBridge(str(config_path))


# === Bridge Message Handling Tests ===


class TestBridgeMessageHandling:
    @pytest.fixture
    def bridge(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text("""
slack:
  app_token: "xapp-test"
  bot_token: "xoxb-test"
threads:
  base_dir: "{}"
kiro:
  cli_path: "kiro-cli"
  agent: ""
acp:
  response_timeout: 300
  max_sessions: 100
""".format(tmp_path / "threads"))

        with patch("bridge.WebClient"), patch("bridge.SocketModeClient"):
            from bridge import KiroSlackBridge
            b = KiroSlackBridge(str(config_path))
            b._loop = asyncio.new_event_loop()
            return b

    def test_ignores_bot_messages(self, bridge):
        bridge.handle_message({"bot_id": "B123", "text": "hi"})
        # Should not crash or schedule anything

    def test_ignores_messages_without_user(self, bridge):
        bridge.handle_message({"text": "hi", "channel": "C1", "ts": "1.2"})

    def test_rate_limit(self, bridge):
        bridge.per_user_limit = 2
        assert bridge.check_rate_limit("U1") is True
        assert bridge.check_rate_limit("U1") is True
        assert bridge.check_rate_limit("U1") is False

    def test_thread_dir_creation(self, bridge):
        thread_dir = bridge.get_thread_dir("1709516874.123456")
        assert thread_dir.exists()
        assert "2024" in str(thread_dir) or "2025" in str(thread_dir)

    def test_send_message_short(self, bridge):
        mock_post = Mock()
        bridge.client.chat_postMessage = mock_post
        bridge.send_message("C1", "1.2", "hello")
        mock_post.assert_called_once()

    def test_send_message_chunking(self, bridge):
        mock_post = Mock()
        bridge.client.chat_postMessage = mock_post
        bridge.send_message("C1", "1.2", "x" * 4000)
        assert mock_post.call_count == 2


# === TurnResult Tests ===


class TestTurnResult:
    def test_defaults(self):
        r = TurnResult()
        assert r.text == ""
        assert r.tool_calls == []
        assert r.stop_reason == ""

    def test_accumulation(self):
        r = TurnResult()
        r.text += "hello "
        r.text += "world"
        assert r.text == "hello world"
