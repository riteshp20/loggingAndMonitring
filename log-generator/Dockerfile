FROM python:3.12-slim

# No external PyPI dependencies — stdlib only.

WORKDIR /app

COPY generator.py .

RUN useradd --system --no-create-home --shell /usr/sbin/nologin appuser \
 && chown -R appuser:appuser /app

USER appuser

# Unbuffered output so logs appear immediately in docker compose logs
ENV PYTHONUNBUFFERED=1

CMD ["python", "generator.py"]
