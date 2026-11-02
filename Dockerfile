FROM python:3.11-slim

WORKDIR /app

COPY fuel_bot/requirements.txt fuel_bot/requirements.txt
RUN pip install --no-cache-dir -r fuel_bot/requirements.txt

COPY fuel_bot ./fuel_bot

ENV CHECK_INTERVAL_SEC=300 \
    MAP_HOST=0.0.0.0

# Render и другие PaaS передают PORT; локально по умолчанию 8080
CMD ["python", "-m", "fuel_bot.main"]
