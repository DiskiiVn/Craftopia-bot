FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libopus0 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py ai_support.py knowledge.py converter.py env_loader.py incident_monitor.py mc_status.py message_cleanup.py music.py ./
COPY knowledge ./knowledge

RUN useradd --create-home --uid 10001 craftopia && chown -R craftopia:craftopia /app
USER craftopia

CMD ["python", "bot.py"]
