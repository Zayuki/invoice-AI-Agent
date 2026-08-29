FROM ghcr.io/astral-sh/uv:0.5-python3.12-bookworm-slim AS builder

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY requirements.txt .
RUN uv venv /opt/venv \
    && uv pip install --python /opt/venv/bin/python --no-cache -r requirements.txt

FROM python:3.12-slim

WORKDIR /app

ENV PATH="/opt/venv/bin:$PATH"

COPY --from=builder /opt/venv /opt/venv
RUN playwright install --with-deps chromium

COPY invoice_agent ./invoice_agent
COPY invoice_template.html .

ENV DATABASE_PATH=/data/invoice_agent.db \
    OUTPUT_DIR=/data/generated

EXPOSE 8000

CMD ["uvicorn", "invoice_agent.main:app", "--host", "0.0.0.0", "--port", "8000"]
