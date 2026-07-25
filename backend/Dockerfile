FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

COPY pyproject.toml ./
COPY agentgate ./agentgate
COPY data ./data

RUN pip install --upgrade pip && pip install -e ".[server,tracing]"

EXPOSE 8080

CMD uvicorn agentgate.main:app --host 0.0.0.0 --port ${PORT}
