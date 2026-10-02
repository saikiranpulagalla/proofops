FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app
RUN addgroup --system proofops && adduser --system --ingroup proofops proofops
COPY pyproject.toml alembic.ini release_identity.json ./
COPY alembic ./alembic
COPY app ./app
COPY scripts ./scripts
RUN pip install --no-cache-dir '.[release]'
USER proofops
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/ready', timeout=3).read()"
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.api.main:app --host 0.0.0.0 --port ${PORT}"]
