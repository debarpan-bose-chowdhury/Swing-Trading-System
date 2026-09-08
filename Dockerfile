# syntax=docker/dockerfile:1
FROM python:3.12-slim

WORKDIR /app

RUN addgroup --system --gid 10001 appgroup \
    && adduser --system --uid 10001 --ingroup appgroup --no-create-home appuser

COPY app ./app

ENV PORT=8080 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8080
USER appuser

CMD ["python", "-m", "app"]
