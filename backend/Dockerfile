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

# Migrations run as a separate step (docker-compose still runs them inline via
# its own command override; Kubernetes runs them as a Helm pre-upgrade Job).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
