# syntax=docker/dockerfile:1
FROM python:3.13-slim AS build
RUN python -m venv /venv
COPY requirements.txt .
RUN /venv/bin/pip install --no-cache-dir -r requirements.txt

FROM python:3.13-slim
# tzdata so TZ (set in compose) makes the 10:30 schedule local time, not UTC
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 quill
COPY --from=build /venv /venv
WORKDIR /app
COPY app.py settings.example.json ./
USER quill
ENV PATH=/venv/bin:$PATH PYTHONUNBUFFERED=1 HOST=0.0.0.0 QUILLWORK_DATA=/data
EXPOSE 8787
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=4)"
CMD ["python", "app.py"]
