# TeleSeek monitoring panel — a separate lightweight image.
# Isolated from the bot image: its own ./panel build context and its own dependencies.
# Reads from the database; listens on 8090 (not exposed publicly — see docker-compose).
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8090

# host 0.0.0.0 — inside the container (normal); the port is published ONLY on the host's
# 127.0.0.1 via docker-compose (same as Adminer/postgres).
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8090"]
