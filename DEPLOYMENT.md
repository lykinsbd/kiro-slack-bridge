# Systemd Service Deployment

## Installation

1. **Create environment file with tokens:**

```bash
sudo mkdir -p /etc/kiro-slack-bridge
sudo nano /etc/kiro-slack-bridge/env
```

Add:
```
SLACK_APP_TOKEN=xapp-your-token
SLACK_BOT_TOKEN=xoxb-your-token
```

Secure it:
```bash
sudo chmod 600 /etc/kiro-slack-bridge/env
```

2. **Install service:**

```bash
sudo cp kiro-slack-bridge@.service /etc/systemd/system/
sudo systemctl daemon-reload
```

3. **Start service (replace `username` with your user):**

```bash
sudo systemctl enable kiro-slack-bridge@username
sudo systemctl start kiro-slack-bridge@username
```

## Management

**Check status:**
```bash
sudo systemctl status kiro-slack-bridge@username
```

**View logs:**
```bash
sudo journalctl -u kiro-slack-bridge@username -f
```

**Restart:**
```bash
sudo systemctl restart kiro-slack-bridge@username
```

**Stop:**
```bash
sudo systemctl stop kiro-slack-bridge@username
```

## Using EnvironmentFile

Edit the service file to use the environment file:

```bash
sudo systemctl edit kiro-slack-bridge@username
```

Add:
```ini
[Service]
EnvironmentFile=/etc/kiro-slack-bridge/env
```

Then reload and restart:
```bash
sudo systemctl daemon-reload
sudo systemctl restart kiro-slack-bridge@username
```
