FROM python:3.12-slim-bookworm

WORKDIR /app

# Install system dependencies for Playwright and supercronic
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install supercronic (Docker-friendly cron)
ARG SUPERCRONIC_VERSION=v0.2.33
RUN curl -fsSL "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64" \
    -o /usr/local/bin/supercronic \
    && chmod +x /usr/local/bin/supercronic

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium with system dependencies
RUN playwright install chromium --with-deps

# Copy app code
COPY src/ src/
COPY crontab /app/crontab
COPY docker-entrypoint.sh /app/docker-entrypoint.sh

# Fix Windows CRLF line endings and set permissions
RUN sed -i 's/\r$//' /app/crontab /app/docker-entrypoint.sh \
    && chmod +x /app/docker-entrypoint.sh

# Create runtime directories
RUN mkdir -p /app/credentials /app/logs

ENTRYPOINT ["/app/docker-entrypoint.sh"]
