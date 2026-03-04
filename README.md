# Kiro Slack Bridge

Slack bot that integrates with Kiro CLI for conversational AI assistance.

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

### 2. Configure Bridge

**Option 1: Environment Variables (Recommended)**

```bash
export SLACK_APP_TOKEN="xapp-..."
export SLACK_BOT_TOKEN="xoxb-..."
```

**Option 2: Local Config File**

```bash
cp config.local.yaml.example config.local.yaml
# Edit config.local.yaml with your tokens
```

Then run with: `uv run python bridge.py config.local.yaml`

### 3. Install Dependencies

```bash
uv sync
```

### 4. Run

```bash
uv run python bridge.py
```

## How It Works

1. Receives messages from Slack via Socket Mode (WebSocket)
2. Creates a unique directory per thread: `{base_dir}/{year}/{month}/{day}/{thread_ts}/`
3. Runs `kiro-cli chat` from that directory
4. Automatically resumes conversations in existing threads
5. Sends Kiro's response back to Slack

## Features

- Thread-based conversations with full context
- Date-organized storage for easy management
- Configurable Kiro agent and trust settings
- Works in channels (via @mention) and DMs
- Message chunking for long responses (>3000 chars)
- 5-minute timeout protection
- Structured logging
- Auto-restart via systemd

## Testing

Run tests:
```bash
uv run pytest test_bridge.py -v
```

## Deployment

See [DEPLOYMENT.md](DEPLOYMENT.md) for systemd service setup.

## Configuration

See `config.yaml` for all options:
- Thread storage location
- Kiro CLI path and agent settings
- Tool trust settings

## Thread Storage

Conversations are stored in: `{base_dir}/{year}/{month}/{day}/{thread_id}/`

Default: `~/kiro-slack-threads/2026/03/03/C12345-1709516874.123456/`
