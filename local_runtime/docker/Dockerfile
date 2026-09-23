# syntax=docker/dockerfile:1
FROM python:3.11-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.12.6 /uv /usr/local/bin/uv

# Keep the paths already recorded in memory metadata stable inside the image.
WORKDIR /Knowin/foundation/seb/mem0-main
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    UV_LINK_MODE=copy \
    MEM0_TELEMETRY=false \
    HF_HUB_OFFLINE=1

# PyAV wheels contain FFmpeg. LibreOffice + fonts provide Word page rendering.
ARG WITH_LIBREOFFICE=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libgomp1 \
    && if [ "$WITH_LIBREOFFICE" = "1" ]; then \
         apt-get install -y --no-install-recommends libreoffice-writer fonts-noto-cjk; \
       fi \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY mem0/ ./mem0/
COPY local_runtime/ ./local_runtime/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv --python /usr/local/bin/python /opt/venv \
    && uv pip sync --python /opt/venv/bin/python local_runtime/requirements.lock \
    && uv pip check --python /opt/venv/bin/python

EXPOSE 18580
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD ["python", "-c", "import json,urllib.request; o=urllib.request.build_opener(urllib.request.ProxyHandler({})); r=json.load(o.open('http://127.0.0.1:18580/api/overview',timeout=8)); assert r['status']=='ready'"]

# Direct foreground server: Docker owns the PID, signals and logs.
# One worker is required for the local Qdrant directory.
CMD ["uvicorn", "local_runtime.container_runtime:create_app", "--factory", "--host", "0.0.0.0", "--port", "18580", "--workers", "1", "--no-access-log", "--no-proxy-headers"]
