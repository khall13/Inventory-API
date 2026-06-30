FROM python:3.11-slim

WORKDIR /app
COPY scripts/requirements.txt scripts/requirements.txt
RUN pip install --no-cache-dir -r scripts/requirements.txt
COPY . .

# Default = the daily job. Railway's cron schedule (railway.json deploy.cronSchedule)
# decides WHEN this runs; the container runs once to completion and exits.
# init-db is idempotent (all DDL is IF NOT EXISTS) so it's safe to run every time.
CMD ["sh", "-c", "python scripts/sync.py --init-db && python scripts/sync.py --domain all --mode incremental --email"]
