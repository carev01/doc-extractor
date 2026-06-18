FROM python:3.13-slim

WORKDIR /app

# WeasyPrint native dependencies (Pango + fonts). Pillow (pip) handles image
# decoding, so no extra image libs are required.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangoft2-1.0-0 libfontconfig1 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Run migrations then start the server.
# For Kubernetes, replace this with an init container for the migration step
# so only one pod runs `alembic upgrade head` per deploy.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
