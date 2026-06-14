# TextBase — Self-hosted, Twilio-compatible SMS Platform

> Free forever. Zero per-message cost. No vendor lock-in. Runs on any free hosting.

TextBase is a full SMS API platform you host yourself. It delivers messages via email-to-SMS carrier gateways — a free, permanent service provided by every major US carrier.

## Quick Start

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env: add your Gmail App Password
python app.py
# Open http://localhost:5000
```

## API

### Send SMS (native)
```bash
curl -X POST http://localhost:5000/api/v1/messages \
  -H "X-Api-Key: sk_your_key" \
  -H "Content-Type: application/json" \
  -d '{"to":"+15551234567","carrier":"verizon","body":"Hello!"}'
```

### Twilio drop-in replacement
```python
from twilio.rest import Client
client = Client("YOUR_ACCOUNT_SID", "YOUR_AUTH_TOKEN")
client.api.base_url = "http://your-textbase-server.com"
client.messages.create(to="+15551234567", from_="+1", body="Hello from TextBase!")
```

## Free Hosting

| Platform | Free tier |
|----------|-----------|
| Oracle Cloud Always Free | 4 OCPUs · 24 GB RAM · forever |
| Fly.io | 3 shared VMs · forever |
| Render | Free (sleeps on inactivity) |
