# Kiro Slack Bridge

Slack bot that integrates with Kiro CLI via [ACP (Agent Client Protocol)](https://agentclientprotocol.com/) for streaming, session-persistent AI assistance.

## 🚀 Quick Start

**New to this project?** → See [GETTING_STARTED.md](GETTING_STARTED.md) for a 5-minute setup guide.

**Ready to deploy?** → See [DEPLOYMENT.md](DEPLOYMENT.md) for production systemd setup.

## Architecture

```
Slack ←WebSocket→ Bridge (ACP Client) ←stdio JSON-RPC→ kiro-cli acp
                      │
                      ├── Thread → ACP Session mapping
                      ├── Streaming chunks → Slack message edits
                      ├── Tool call notifications → status indicators
                      └── Session persistence (resume conversations)
```

The bridge communicates with Kiro CLI using the [Agent Client Protocol](https://agentclientprotocol.com/), an open standard for agent-editor communication (like LSP for AI agents). This provides:

- **Streaming responses** — messages update in real-time as Kiro generates output
- **Session persistence** — conversations continue across messages in a thread
- **Structured tool visibility** — see what tools Kiro is using (🔧 Reading file...)
- **Single long-lived process** — no subprocess spawn overhead per message

## Setup

### 1. Create Slack App

1. Go to https://api.slack.com/apps
2. Click "Create New App" → "From scratch"
3. Name it (e.g., "Kiro Assistant") and select your workspace
4. Enable Socket Mode:
   - Settings → Socket Mode → Enable
   - Generate an App-Level Token with `connections:write` scope
   - Save as `SLACK_APP_TOKEN`
5. Add Bot Token Scopes (OAuth & Permissions):
   - `app_mentions:read`
   - `chat:write`
   - `channels:history`
   - `groups:history`
   - `im:history`
6. Install app to workspace and save the Bot Token as `SLACK_BOT_TOKEN`
7. Enable Event Subscriptions:
   - Subscribe to bot events: `app_mention`, `message.channels`, `message.groups`, `message.im`
8. Enable Messages Tab (App Home):
   - Toggle ON "Allow users to send Slash commands and messages from the messages tab"
9. Enable Slash Commands (optional):
   - `/kiro-reset` — Reset conversation in a thread
   - `/kiro-help` — Show help

### 2. Prerequisites

- **Kiro CLI 2.0+** with ACP support (`kiro-cli acp` command)
- **KIRO_API_KEY** environment variable for headless authentication
- Python 3.11+

### 3. Configure Bridge

```bash
cp config.local.yaml.example config.local.yaml
# Edit config.local.yaml with your Slack tokens
```

Or use environment variables:
```bash
export SLACK_APP_TOKEN="xapp-..."
export SLACK_BOT_TOKEN="xoxb-..."
export KIRO_API_KEY="..."
```

### 4. Install & Run

```bash
uv sync
uv run python bridge.py config.local.yaml
```

## How It Works

1. Receives messages from Slack via Socket Mode (WebSocket)
2. Spawns a single `kiro-cli acp` process (persistent, long-lived)
3. Creates an ACP session per Slack thread (mapped via `session_store.py`)
4. Sends prompts via JSON-RPC, streams response chunks back to Slack
5. Edits the Slack message in real-time as chunks arrive
6. Resumes existing sessions when users continue a thread

## Features

- **ACP-based communication** — structured JSON-RPC protocol, no stdout parsing
- **Streaming responses** — real-time message updates in Slack
- **Session persistence** — full conversation context maintained per thread
- **Tool call visibility** — shows what Kiro is doing (reading files, running commands)
- **Thread-based conversations** — each thread gets its own ACP session
- **Rate limiting** — per-user and global concurrency limits
- **Health check & metrics** — `:9090/health`, `:9090/metrics`
- **Slash commands** — `/kiro-reset`, `/kiro-help`
- **LRU session management** — automatic eviction of old sessions
- **Auto-restart** — via systemd (see DEPLOYMENT.md)

## Configuration

See `config.local.yaml.example` for all options:

```yaml
slack:
  app_token: "xapp-..."
  bot_token: "xoxb-..."

threads:
  base_dir: "~/kiro-slack-threads"

kiro:
  cli_path: ""       # defaults to "kiro-cli"
  agent: ""          # optional custom agent

acp:
  response_timeout: 300   # max seconds per turn
  max_sessions: 100       # LRU eviction limit

rate_limits:
  per_user_per_minute: 10
  max_concurrent: 3

health:
  port: 9090
```

## Testing

```bash
uv run pytest test_bridge.py -v
```

## Project Structure

```
bridge.py              — Main Slack bridge with event handling
acp_client.py          — ACP client (wraps kiro-cli acp via JSON-RPC)
session_store.py       — Thread→Session mapping with LRU persistence
config.local.yaml.example — Configuration template
test_bridge.py         — Test suite
```

## Deployment

See [DEPLOYMENT.md](DEPLOYMENT.md) for systemd service setup.
