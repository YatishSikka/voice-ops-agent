# The agent process: Telegram polling loop plus the local callback server.
# n8n runs as its own container -- see docker-compose.yml.
FROM python:3.11-slim

WORKDIR /app

# Dependencies first, so code edits do not reinstall the world.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py callback_api.py bot.py ./
COPY agent/ ./agent/
COPY tools/ ./tools/
COPY tasks/ ./tasks/
COPY tgbot/ ./tgbot/

# The callback server binds here for n8n to reach it over the compose network.
EXPOSE 7860

# Unbuffered, or logs sit in a buffer and `docker logs` looks dead.
ENV PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
