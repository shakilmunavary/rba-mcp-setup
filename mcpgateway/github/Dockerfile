
FROM python:3.12-slim

ARG GITHUB_URL="https://api.github.com"
ARG GITHUB_PAT=""
ARG GITHUB_ORG=""
ARG GITHUB_VERIFY_SSL="true"
ARG MCP_SECRET_TOKEN="1234456789"
ARG HOST="0.0.0.0"
ARG PORT="6501"

ENV GITHUB_URL=${GITHUB_URL} \
    GITHUB_PAT=${GITHUB_PAT} \
    GITHUB_ORG=${GITHUB_ORG} \
    GITHUB_VERIFY_SSL=${GITHUB_VERIFY_SSL} \
    MCP_SECRET_TOKEN=${MCP_SECRET_TOKEN} \
    HOST=${HOST} \
    PORT=${PORT} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN pip install --no-cache-dir aiohttp==3.10.11

COPY github_mcp_server.py .

EXPOSE ${PORT}

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python3 -c \
    "import urllib.request,os,sys; \
     p=os.environ.get('PORT','6501'); \
     r=urllib.request.urlopen(f'http://localhost:{p}/health',timeout=8); \
     sys.exit(0 if r.status==200 else 1)"

RUN useradd --no-create-home --shell /bin/false mcpuser
USER mcpuser

CMD ["python3", "github_mcp_server.py"]
