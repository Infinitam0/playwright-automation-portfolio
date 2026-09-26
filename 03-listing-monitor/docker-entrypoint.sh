#!/bin/bash
set -e

# Decode base64-encoded Google credentials if provided
if [ -n "$LISTING_GOOGLE_CREDENTIALS_B64" ]; then
    echo "[entrypoint] Decoding Google credentials..."
    mkdir -p /app/credentials
    python -c "import base64,os; open('/app/credentials/service_account.json','wb').write(base64.b64decode(os.environ['LISTING_GOOGLE_CREDENTIALS_B64']))"
    export LISTING_GOOGLE_CREDENTIALS_PATH=/app/credentials/service_account.json
fi

# Run the scraper once on startup (catches up on any missed runs)
echo "[entrypoint] Running initial scrape..."
cd /app && python -u src/main.py 2>&1 || echo "[entrypoint] Initial scrape failed (non-fatal), continuing with scheduled runs"

# Start supercronic scheduler
echo "[entrypoint] Starting scheduler (4x daily: 06:00, 11:00, 16:00, 21:00 UTC)..."
exec supercronic --no-reap /app/crontab
