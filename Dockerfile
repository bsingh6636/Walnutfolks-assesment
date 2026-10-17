FROM python:3.13-slim

WORKDIR /app

# Do not write .pyc files; flush logs straight to the console.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

# Install dependencies first so this layer is cached between code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

# Hosting platforms set PORT; fall back to 8000 for local docker runs.
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
