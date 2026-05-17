
FROM python:3.12-slim

ARG GITLAB_URL="https://gitlab.com"
ARG GITLAB_TOKEN=""
ARG GITLAB_VERIFY_SSL="true"
ARG MCP_SECRET_TOKEN="1234456789"
ARG HOST="0.0.0.0"
ARG PORT="6502"

ENV GITLAB_URL=${GITLAB_URL} \
    GITLAB_TOKEN=${GITLAB_TOKEN} \
    GITLAB_VERIFY_SSL=${GITLAB_VERIFY_SSL} \
    MCP_SECRET_TOKEN=${MCP_SECRET_TOKEN} \
    HOST=${HOST} \
    PORT=${PORT} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN pip install --no-cache-dir aiohttp==3.10.11

COPY gitlab_mcp_server.py .

EXPOSE ${PORT}

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python3 -c \
    "import urllib.request,os,sys; \
     p=os.environ.get('PORT','6502'); \
     r=urllib.request.urlopen(f'http://localhost:{p}/health',timeout=8); \
     sys.exit(0 if r.status==200 else 1)"

RUN useradd --no-create-home --shell /bin/false mcpuser
USER mcpuser

CMD ["python3", "gitlab_mcp_server.py"]
