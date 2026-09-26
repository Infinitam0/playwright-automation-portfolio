# Permission bot - Docker image
# Uses the official Playwright Python image with browsers pre-installed
#
# Build:  docker build -t permission-bot .
# Run:    docker compose up --build

FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

WORKDIR /app

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy the automation script and its UI labels
COPY permission_bot.py labels.py ./

# permissions.xlsx is mounted at runtime via docker-compose (not baked into image)

# Default to headless mode (required in Docker - no display available)
ENV HEADLESS=true

CMD ["python", "permission_bot.py"]
