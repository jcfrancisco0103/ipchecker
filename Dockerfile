FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WATCHES_PATH=/data/watches.json

RUN apt-get update \
    && apt-get install -y --no-install-recommends iputils-ping libcap2-bin \
    && rm -rf /var/lib/apt/lists/* \
    && setcap cap_net_raw+ep "$(command -v ping)"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin bot \
    && mkdir -p /data \
    && chown bot:bot /data

USER bot

CMD ["python", "-u", "bot.py"]
