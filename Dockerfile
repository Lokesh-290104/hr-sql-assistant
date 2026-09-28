# Demo image: the app plus a generated SQLite HR database built in at image build time.
# The app opens that file read-only (see app/db.py), on top of the SQL guardrails.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app app
COPY scripts scripts
COPY static static

# Synthetic data only (Faker); no real HR records are ever in the image.
RUN python -m scripts.seed --url sqlite:////app/hr_demo.db \
    && useradd --system --no-create-home app

USER app
EXPOSE 8000
# Render (and most hosts) pass the port in $PORT.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
