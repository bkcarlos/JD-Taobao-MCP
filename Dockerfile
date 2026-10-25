FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYWRIGHT_HEADLESS=true \
    BROWSER_PROFILE_DIR=/data/browser-profile \
    MCP_HTTP_HOST=0.0.0.0 \
    MCP_HTTP_PORT=8000

# Match the installed Python package to the browser revision downloaded here.
RUN pip install --no-cache-dir playwright==1.64.1 \
    && python -m playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /ms-playwright

WORKDIR /app
COPY pyproject.toml README.md LICENSE server.py ./
COPY jd_taobao_mcp ./jd_taobao_mcp
RUN pip install --no-cache-dir . \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin mcp \
    && install -d -o mcp -g mcp -m 0700 /data
COPY tests ./tests

USER 10001:10001
EXPOSE 8000
CMD ["python", "server.py", "--http"]
