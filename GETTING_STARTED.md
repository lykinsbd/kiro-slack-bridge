# Getting Started with Kiro Slack Bridge

## Prerequisites
- Kiro CLI installed and working (`kiro-cli --version`)
- Slack workspace where you can create apps
- Python 3.11+ (managed by uv)

## Quick Start (5 minutes)

### 1. Clone and Setup
```bash
cd /opt/venvs/kiro-slack-bridge
uv sync
```

### 2. Create Slack App
1. Go to https://api.slack.com/apps → **Create New App** → **From scratch**
2. Name it "Kiro Assistant" and select your workspace
3. **Enable Socket Mode:**
   - Settings → Socket Mode → Toggle ON
   - Click "Generate Token" with `connections:write` scope
   - Copy the token (starts with `xapp-`)
4. **Add Bot Scopes** (OAuth & Permissions):
   - `app_mentions:read`
   - `chat:write`
   - `channels:history`
   - `groups:history`
   - `im:history`
5. **Install to Workspace:**
   - Click "Install to Workspace"
   - Copy the Bot User OAuth Token (starts with `xoxb-`)
6. **Enable Events** (Event Subscriptions):
   - Toggle ON
   - Subscribe to: `app_mention`, `message.channels`, `message.groups`, `message.im`
7. **Add Slash Commands** (optional):
   - Create: `/kiro-reset`, `/kiro-status`, `/kiro-help`
   - Request URL: leave blank (Socket Mode doesn't need it)

### 3. Configure Tokens
```bash
export SLACK_APP_TOKEN="xapp-1-A0..."
export SLACK_BOT_TOKEN="xoxb-123..."
```

Or create `config.local.yaml`:
```yaml
slack:
  app_token: "xapp-1-A0..."
  bot_token: "xoxb-123..."
threads:
  base_dir: "~/kiro-slack-threads"
kiro:
  cli_path: ""
  agent: ""
  trust_all_tools: false
rate_limits:
  per_user_per_minute: 10
  max_concurrent: 3
```

### 4. Run the Bridge
```bash
# With environment variables
uv run python bridge.py

# Or with config file
uv run python bridge.py config.local.yaml
```

You should see:
```
🏥 Health check server started on :8080
🚀 Kiro Slack Bridge starting...
📁 Thread storage: /home/user/kiro-slack-threads
⚡ Rate limit: 10 msgs/min per user
🔄 Max concurrent: 3 processes
✅ Connected to Slack
```

### 5. Test It
1. In Slack, mention the bot: `@Kiro Assistant hello!`
2. Or DM the bot directly
3. Try slash commands: `/kiro-help`

## Verify It's Working

**Check health:**
```bash
curl http://localhost:8080/health
# {"status":"healthy"}
```

**Check metrics:**
```bash
curl http://localhost:8080/metrics
# {"uptime_seconds": 42, "messages_processed": 5, ...}
```

**Check logs:**
```bash
# Look for "Received message from..." and "Sent response to..."
```

## Production Deployment

For production use, see [DEPLOYMENT.md](DEPLOYMENT.md) for systemd service setup.

## Troubleshooting

**"Slack tokens not configured"**
- Make sure tokens are exported or in `config.local.yaml`
- Tokens should start with `xapp-` and `xoxb-`

**Bot doesn't respond**
- Check Event Subscriptions are enabled in Slack app
- Verify bot is invited to the channel (`/invite @Kiro Assistant`)
- Check logs for errors

**"Permission denied" errors**
- Ensure bot has required scopes in OAuth & Permissions
- Reinstall app to workspace after adding scopes

## Next Steps
- Configure rate limits in `config.yaml`
- Set up systemd service for auto-start
- Monitor metrics endpoint
- Customize Kiro agent settings
