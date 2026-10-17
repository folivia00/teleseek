FROM python:3.11-slim

WORKDIR /app

# Unprivileged user
RUN groupadd -r botuser && useradd -r -g botuser botuser

# Dependencies in a separate layer (cached). INSTALL_DEV=1 adds the test tools.
ARG INSTALL_DEV=0
COPY requirements.txt requirements-dev.txt ./
RUN if [ "$INSTALL_DEV" = "1" ]; then pip install --no-cache-dir -r requirements-dev.txt; \
    else pip install --no-cache-dir -r requirements.txt; fi

# Source code
COPY . .
RUN chown -R botuser:botuser /app

USER botuser

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# Start the bot
CMD ["python", "-m", "bot.main"]
