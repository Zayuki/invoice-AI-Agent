FROM docker.io/library/python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY invoice_agent ./invoice_agent
COPY invoice_template.html .

ENV DATABASE_PATH=/data/invoice_agent.db \
    OUTPUT_DIR=/data/generated

EXPOSE 8000

CMD ["uvicorn", "invoice_agent.main:create_configured_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
