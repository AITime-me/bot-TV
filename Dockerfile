FROM python:3.12.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd --system bot-tv \
    && useradd --system --gid bot-tv --home-dir /nonexistent bot-tv

COPY requirements-lock.txt .
RUN python -m pip install --no-cache-dir -r requirements-lock.txt

COPY alembic.ini .
COPY alembic ./alembic
COPY app ./app

USER bot-tv

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
