FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src/ ./src/

COPY entrypoint.sh /entrypoint.sh

RUN pip install --no-cache-dir . \
    && useradd --no-create-home --shell /bin/false bga \
    && mkdir -p /data && chown bga:bga /data \
    && chown bga:bga /app \
    && chmod +x /entrypoint.sh

USER bga

ENV BGA_DB_PATH=/data/bga_bot.db

VOLUME ["/data"]

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "bga_turn"]
