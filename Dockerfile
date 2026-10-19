FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Зависимости отдельным слоем: пересобираются только при изменении requirements.txt.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini .
COPY migrations ./migrations
COPY app ./app

RUN useradd --create-home --uid 10001 bot
USER bot

# Миграции Railway запускает pre-deploy командой (.railway/railway.ts).
# Для запуска вне Railway: docker run ... sh -c "alembic upgrade head && python -m app.main"
CMD ["python", "-m", "app.main"]
