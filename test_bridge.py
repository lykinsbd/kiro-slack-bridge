"""Unit tests for Kiro Slack Bridge"""

import os
import pytest
from pathlib import Path
from datetime import datetime
from unittest.mock import Mock, patch
from bridge import KiroSlackBridge


@pytest.fixture
def mock_config(tmp_path):
    """Create a mock config file"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
slack:
  app_token: "xapp-test"
  bot_token: "xoxb-test"

threads:
  base_dir: "{}"

kiro:
  cli_path: "kiro-cli"
  agent: ""
  trust_all_tools: false
""".format(tmp_path / "threads")
    )
    return str(config_path)


@pytest.fixture
def bridge(mock_config):
    """Create a bridge instance with mocked Slack clients"""
    with patch("bridge.WebClient"), patch("bridge.SocketModeClient"):
        return KiroSlackBridge(mock_config)


class TestConfig:
    def test_config_loading(self, bridge):
        """Test config loads correctly"""
        assert bridge.app_token == "xapp-test"
        assert bridge.bot_token == "xoxb-test"
        assert bridge.kiro_cli == "kiro-cli"

    def test_env_var_expansion(self, tmp_path):
        """Test environment variable expansion in config"""
        os.environ["TEST_TOKEN"] = "xapp-from-env"
        config_path = tmp_path / "config.yaml"
        config_path.write_text("""
slack:
  app_token: "${TEST_TOKEN}"
  bot_token: "xoxb-test"
threads:
  base_dir: "~/threads"
kiro:
  cli_path: ""
  agent: ""
  trust_all_tools: false
""")

        with patch("bridge.WebClient"), patch("bridge.SocketModeClient"):
            bridge = KiroSlackBridge(str(config_path))
            assert bridge.app_token == "xapp-from-env"

    def test_missing_tokens_raises_error(self, tmp_path):
        """Test that missing tokens raise ValueError"""
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
  trust_all_tools: false
""")

        with patch("bridge.WebClient"), patch("bridge.SocketModeClient"):
            with pytest.raises(ValueError, match="Slack tokens not configured"):
                KiroSlackBridge(str(config_path))


class TestThreadDirectory:
    def test_get_thread_dir_creates_path(self, bridge):
        """Test thread directory path generation"""
        thread_ts = "1709516874.123456"
        thread_dir = bridge.get_thread_dir(thread_ts)

        dt = datetime.fromtimestamp(float(thread_ts))
        expected = (
            bridge.base_dir
            / str(dt.year)
            / f"{dt.month:02d}"
            / f"{dt.day:02d}"
            / thread_ts
        )

        assert thread_dir == expected
        assert thread_dir.exists()


class TestKiroCLI:
    def test_run_kiro_success(self, bridge, mocker):
        """Test successful Kiro CLI execution"""
        mock_run = mocker.patch("bridge.subprocess.run")
        mock_run.return_value = Mock(returncode=0, stdout="Response from Kiro")

        response = bridge.run_kiro("test message", Path("/tmp/test"))
        assert response == "Response from Kiro"

        # Verify command structure
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert cmd[0] == "kiro-cli"
        assert cmd[1] == "chat"
        assert cmd[2] == "--no-interactive"
        assert cmd[3] == "--resume"
        assert "test message" in cmd

    def test_run_kiro_with_agent(self, bridge, mocker):
        """Test Kiro CLI with agent setting"""
        bridge.agent = "test-agent"
        mock_run = mocker.patch("bridge.subprocess.run")
        mock_run.return_value = Mock(returncode=0, stdout="Response")

        bridge.run_kiro("test", Path("/tmp/test"))

        cmd = mock_run.call_args[0][0]
        assert "--agent" in cmd
        assert "test-agent" in cmd

    def test_run_kiro_timeout(self, bridge, mocker):
        """Test Kiro CLI timeout handling"""
        import subprocess

        mock_run = mocker.patch("bridge.subprocess.run")
        mock_run.side_effect = subprocess.TimeoutExpired("kiro-cli", 300)

        response = bridge.run_kiro("test", Path("/tmp/test"))
        assert "timeout" in response.lower()

    def test_run_kiro_error(self, bridge, mocker):
        """Test Kiro CLI error handling"""
        mock_run = mocker.patch("bridge.subprocess.run")
        mock_run.return_value = Mock(returncode=1, stderr="Error occurred")

        response = bridge.run_kiro("test", Path("/tmp/test"))
        assert "error" in response.lower()


class TestMessageHandling:
    def test_send_message_short(self, bridge, mocker):
        """Test sending short message"""
        mock_post = mocker.patch.object(bridge.client, "chat_postMessage")

        bridge.send_message("C123", "1234.5678", "Short message")

        mock_post.assert_called_once()
        assert mock_post.call_args[1]["text"] == "Short message"

    def test_send_message_chunking(self, bridge, mocker):
        """Test message chunking for long responses"""
        mock_post = mocker.patch.object(bridge.client, "chat_postMessage")

        long_message = "x" * 4000
        bridge.send_message("C123", "1234.5678", long_message)

        # Should be called twice (4000 chars > 3000 limit)
        assert mock_post.call_count == 2

        # Check first chunk has part indicator
        first_call = mock_post.call_args_list[0]
        assert "(Part 1/2)" in first_call[1]["text"]

    def test_handle_message_ignores_bots(self, bridge):
        """Test that bot messages are ignored"""
        event = {"bot_id": "B123", "text": "bot message"}

        # Should return early without processing
        bridge.handle_message(event)
        # No assertions needed - just shouldn't crash

    def test_handle_message_processes_user_message(self, bridge, mocker):
        """Test processing user message"""
        mocker.patch.object(bridge, "get_thread_dir", return_value=Path("/tmp/test"))
        mocker.patch.object(bridge, "run_kiro", return_value="Response")
        mocker.patch.object(bridge, "send_message")

        mock_post = mocker.patch.object(bridge.client, "chat_postMessage")
        mock_post.return_value = {"ts": "1234.5678"}
        mocker.patch.object(bridge.client, "chat_delete")

        event = {"text": "Hello", "channel": "C123", "ts": "1234.5678", "user": "U123"}

        bridge.handle_message(event)

        # Verify Kiro was called
        bridge.run_kiro.assert_called_once()
        # Verify response was sent
        bridge.send_message.assert_called_once()
