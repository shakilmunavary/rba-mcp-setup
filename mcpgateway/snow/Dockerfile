
FROM python:3.11-slim

ARG SNOW_URL=""
ARG SNOW_USERNAME="admin"
ARG SNOW_PASSWORD=""
ARG SNOW_VERIFY_SSL="true"
ARG MCP_SECRET_TOKEN="1234456789"
ARG HOST="0.0.0.0"
ARG PORT="6503"

ENV SNOW_URL=${SNOW_URL} \
    SNOW_USERNAME=${SNOW_USERNAME} \
    SNOW_PASSWORD=${SNOW_PASSWORD} \
    SNOW_VERIFY_SSL=${SNOW_VERIFY_SSL} \
    MCP_SECRET_TOKEN=${MCP_SECRET_TOKEN} \
    HOST=${HOST} \
    PORT=${PORT} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN pip install --no-cache-dir aiohttp==3.10.11

COPY snow_mcp_server.py .

EXPOSE ${PORT}

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python3 -c \
    "import urllib.request,os,sys; \
     p=os.environ.get('PORT','6503'); \
     r=urllib.request.urlopen(f'http://localhost:{p}/health',timeout=8); \
     sys.exit(0 if r.status==200 else 1)"

RUN useradd --no-create-home --shell /bin/false mcpuser
USER mcpuser

CMD ["python3", "snow_mcp_server.py"]
