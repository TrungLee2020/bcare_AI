FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --requirement requirements.txt

COPY app ./app
COPY prompts ./prompts
COPY scripts ./scripts

RUN addgroup --system app \
    && adduser --system --ingroup app app \
    && chown -R app:app /app

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]

# --timeout-graceful-shutdown: stream SSE không bao giờ tự kết thúc, không có
# trần này thì uvicorn chờ chúng mãi và lifespan shutdown (dừng consumer, rời
# consumer group) không bao giờ chạy trước SIGKILL. Hết trần, kết nối SSE bị
# cắt và EventSource tự nối sang instance khác.
# --no-access-log: access log của uvicorn ghi cả query string, tức cả token
# của /chat/stream?token=... Log truy cập để reverse proxy lo (lọc query).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--timeout-graceful-shutdown", "10", "--no-access-log"]
