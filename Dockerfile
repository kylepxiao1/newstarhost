FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y ffmpeg awscli curl xvfb supervisor \
    && rm -rf /var/lib/apt/lists/*

ARG SUPERCRONIC_VERSION=v0.2.38
RUN curl -fsSLo /usr/local/bin/supercronic \
    "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64" \
    && chmod +x /usr/local/bin/supercronic

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m playwright install --with-deps webkit chromium firefox

COPY . .
COPY deploy/supervisord.conf /etc/supervisor/supervisord.conf

ENV PORT=8080
EXPOSE 8080

CMD ["supervisord", "-c", "/etc/supervisor/supervisord.conf", "-n"]
