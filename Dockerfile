FROM python:3.11-slim

WORKDIR /app
COPY scripts/requirements.txt scripts/requirements.txt
RUN pip install --no-cache-dir -r scripts/requirements.txt
COPY . .

# Role-based entrypoint: $SERVICE_ROLE picks web (uvicorn) vs sync (ELT job).
# Same image serves both Railway services; see entrypoint.sh.
CMD ["sh", "entrypoint.sh"]
