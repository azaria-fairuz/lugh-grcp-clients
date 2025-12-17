FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /install
COPY requirements.txt .
RUN pip install uv
RUN uv pip install --no-cache-dir --prefix=/install/packages -r requirements.txt

FROM python:3.12-slim

WORKDIR /app
COPY --from=builder /install/packages /usr/local
COPY . .
COPY ./.env.example ./.env

EXPOSE 8500

CMD ["fastapi", "run", "app.py", "--port=8500"]
