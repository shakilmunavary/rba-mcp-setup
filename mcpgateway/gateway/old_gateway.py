
#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║              MCP Gateway  —  Single Entry Point                  ║
║                                                                  ║
║  Aggregates all MCP backends into one endpoint on port 6000      ║
║  Routes tool calls by prefix to the right backend server         ║
║                                                                  ║
║  Backends:                                                       ║
║    jenkins_*  → jenkins-mcp:6500                                ║
║    github_*   → github-mcp:6501                                 ║
║    gitlab_*   → gitlab-mcp:6502                                 ║
║    snow_*     → snow-mcp:6503                                   ║
║    sonar_*    → sonar-mcp:6504                                  ║
║                                                                  ║
║  Transports: SSE (legacy) + Streamable HTTP (MCP 2025-03-26)    ║
║  Auth: Bearer token                                              ║
╚══════════════════════════════════════════════════════════════════╝
"""

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Optional

import aiohttp
from aiohttp import web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("mcp-gateway")

# ══════════════════════════════════════════════════════════════════
#  CONFIGURATION
# ══════════════════════════════════════════════════════════════════

CONFIG = {
    "HOST":             os.environ.get("HOST",             "0.0.0.0"),
    "PORT":             int(os.environ.get("PORT",         "6000")),
    "MCP_SECRET_TOKEN": os.environ.get("MCP_SECRET_TOKEN", "1234456789"),
    "TOOL_CACHE_TTL":   int(os.environ.get("TOOL_CACHE_TTL", "300")),   # seconds
}

BACKENDS: dict[str, dict] = {
    "jenkins": {
        "url":    os.environ.get("JENKINS_MCP_URL",   "http://jenkins-mcp:6500"),
        "token":  os.environ.get("JENKINS_MCP_TOKEN", "1234456789"),
        "prefix": "jenkins_",
        "color":  "🔵",
    },
    "github": {
        "url":    os.environ.get("GITHUB_MCP_URL",    "http://github-mcp:6501"),
        "token":  os.environ.get("GITHUB_MCP_TOKEN",  "1234456789"),
        "prefix": "github_",
        "color":  "⚫",
    },
    "gitlab": {
        "url":    os.environ.get("GITLAB_MCP_URL",    "http://gitlab-mcp:6502"),
        "token":  os.environ.get("GITLAB_MCP_TOKEN",  "1234456789"),
        "prefix": "gitlab_",
        "color":  "🟠",
    },
    "snow": {
        "url":    os.environ.get("SNOW_MCP_URL",      "http://snow-mcp:6503"),
        "token":  os.environ.get("SNOW_MCP_TOKEN",    "1234456789"),
        "prefix": "snow_",
        "color":  "🟢",
    },
    "sonar": {
        "url":    os.environ.get("SONAR_MCP_URL",     "http://sonar-mcp:6504"),
        "token":  os.environ.get("SONAR_MCP_TOKEN",   "1234456789"),
        "prefix": "sonar_",
        "color":  "🟡",
    },
}

# ══════════════════════════════════════════════════════════════════
#  TOOL CACHE
# ══════════════════════════════════════════════════════════════════

_tool_cache:      list[dict]  = []
_tool_cache_ts:   float       = 0.0
_tool_to_backend: dict[str, str] = {}   # tool_name → backend_name
_backend_status:  dict[str, str] = {}   # backend_name → "ok" | "error: ..."


async def _fetch_backend_tools(
    session: aiohttp.ClientSession,
    name: str,
    backend: dict,
) -> list[dict]:
    url     = backend["url"] + "/mcp"
    headers = {
        "Authorization": f"Bearer {backend['token']}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    try:
        async with session.post(
            url, json=body, headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data  = await resp.json(content_type=None)
            tools = data.get("result", {}).get("tools", [])
            _backend_status[name] = "ok"
            log.info(f"[{name}] fetched {len(tools)} tools")
            return tools
    except Exception as exc:
        _backend_status[name] = f"error: {exc}"
        log.warning(f"[{name}] tool fetch failed — {exc}")
        return []


async def refresh_tools() -> None:
    global _tool_cache, _tool_cache_ts, _tool_to_backend

    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        results = await asyncio.gather(
            *[_fetch_backend_tools(session, n, b) for n, b in BACKENDS.items()],
            return_exceptions=True,
        )

    tools: list[dict]    = []
    mapping: dict[str, str] = {}
    for (name, _), result in zip(BACKENDS.items(), results):
        if isinstance(result, list):
            for t in result:
                tools.append(t)
                mapping[t["name"]] = name

    _tool_cache      = tools
    _tool_to_backend = mapping
    _tool_cache_ts   = time.monotonic()
    log.info(f"Tool cache updated: {len(tools)} total tools across {len(BACKENDS)} backends")


async def get_tools() -> list[dict]:
    if not _tool_cache or (time.monotonic() - _tool_cache_ts) > CONFIG["TOOL_CACHE_TTL"]:
        await refresh_tools()
    return _tool_cache


def resolve_backend(tool_name: str) -> Optional[str]:
    """Return backend name for a tool — cache first, then prefix fallback."""
    if tool_name in _tool_to_backend:
        return _tool_to_backend[tool_name]
    for name, backend in BACKENDS.items():
        if tool_name.startswith(backend["prefix"]):
            return name
    return None


# ══════════════════════════════════════════════════════════════════
#  BACKEND PROXY CALL
# ══════════════════════════════════════════════════════════════════

async def proxy_tool_call(backend_name: str, tool_name: str, arguments: dict) -> dict:
    backend = BACKENDS[backend_name]
    url     = backend["url"] + "/mcp"
    headers = {
        "Authorization": f"Bearer {backend['token']}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }
    payload = {
        "jsonrpc": "2.0",
        "id":      str(uuid.uuid4()),
        "method":  "tools/call",
        "params":  {"name": tool_name, "arguments": arguments},
    }
    try:
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.post(
                url, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                data = await resp.json(content_type=None)
                return data.get("result", {
                    "content": [{"type": "text", "text": "Empty response from backend"}],
                    "isError": True,
                })
    except Exception as exc:
        log.error(f"Backend proxy error [{backend_name}/{tool_name}]: {exc}")
        return {
            "content": [{"type": "text", "text": f"Gateway proxy error: {exc}"}],
            "isError": True,
        }


# ══════════════════════════════════════════════════════════════════
#  JSON-RPC HANDLER
# ══════════════════════════════════════════════════════════════════

def _ok(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}

def _err(req_id, code, msg):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": msg}}


async def dispatch_rpc(body: dict) -> dict:
    method = body.get("method", "")
    params = body.get("params", {})
    req_id = body.get("id")

    # ── Handshake ─────────────────────────────────────────────────
    if method == "initialize":
        return _ok(req_id, {
            "protocolVersion": "2025-03-26",
            "capabilities":    {"tools": {}},
            "serverInfo":      {"name": "mcp-gateway", "version": "1.0.0"},
        })

    if method in ("notifications/initialized", "notifications/cancelled", "ping"):
        return _ok(req_id, {})

    # ── Tool discovery ────────────────────────────────────────────
    if method == "tools/list":
        tools  = await get_tools()
        cursor = params.get("cursor")
        start  = int(cursor) if cursor else 0
        chunk  = tools[start: start + 50]
        nxt    = str(start + 50) if start + 50 < len(tools) else None
        result = {"tools": chunk}
        if nxt:
            result["nextCursor"] = nxt
        return _ok(req_id, result)

    # ── Tool call ─────────────────────────────────────────────────
    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        backend_name = resolve_backend(tool_name)
        if not backend_name:
            # Try once more after a fresh cache refresh
            await refresh_tools()
            backend_name = resolve_backend(tool_name)

        if not backend_name:
            return _ok(req_id, {
                "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
                "isError": True,
            })

        log.info(f"Route  {tool_name}  →  {backend_name}")
        result = await proxy_tool_call(backend_name, tool_name, arguments)
        return _ok(req_id, result)

    return _err(req_id, -32601, f"Method not found: {method}")


# ══════════════════════════════════════════════════════════════════
#  AUTH MIDDLEWARE
# ══════════════════════════════════════════════════════════════════

@web.middleware
async def auth_middleware(request: web.Request, handler):
    if request.path in ("/health", "/", "/metrics") or request.method == "OPTIONS":
        return await handler(request)
    token = CONFIG["MCP_SECRET_TOKEN"].strip()
    if token and request.headers.get("Authorization", "") != f"Bearer {token}":
        return web.json_response({"error": "Unauthorized"}, status=401)
    return await handler(request)


# ══════════════════════════════════════════════════════════════════
#  HTTP HANDLERS
# ══════════════════════════════════════════════════════════════════

async def handle_health(request: web.Request):
    """Health check — pings each backend /health endpoint."""
    backend_health: dict[str, dict] = {}
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        for name, backend in BACKENDS.items():
            try:
                async with session.get(
                    backend["url"] + "/health",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    body = await resp.json(content_type=None)
                    backend_health[name] = {
                        "status": "ok" if resp.status == 200 else f"HTTP {resp.status}",
                        "tools":  body.get("tools", 0),
                        "url":    backend["url"],
                    }
            except Exception as exc:
                backend_health[name] = {
                    "status": f"error: {exc}",
                    "tools":  0,
                    "url":    backend["url"],
                }

    tool_counts = {
        name: sum(1 for t in _tool_cache if t["name"].startswith(BACKENDS[name]["prefix"]))
        for name in BACKENDS
    }

    return web.json_response({
        "status":       "ok",
        "server":       "mcp-gateway",
        "port":         CONFIG["PORT"],
        "total_tools":  len(_tool_cache),
        "tool_counts":  tool_counts,
        "backends":     backend_health,
        "cache_age_s":  round(time.monotonic() - _tool_cache_ts, 1),
    })


async def handle_metrics(request: web.Request):
    """Minimal Prometheus-style metrics."""
    lines = [f"mcp_gateway_total_tools {len(_tool_cache)}"]
    for name in BACKENDS:
        count = sum(1 for t in _tool_cache if t["name"].startswith(BACKENDS[name]["prefix"]))
        lines.append(f'mcp_gateway_backend_tools{{backend="{name}"}} {count}')
    return web.Response(text="\n".join(lines) + "\n", content_type="text/plain")


# ── Session stores ────────────────────────────────────────────────
_sse_sessions: dict[str, asyncio.Queue] = {}
_mcp_sessions: dict[str, asyncio.Queue] = {}


async def handle_sse(request: web.Request):
    """Legacy SSE transport."""
    sid = str(uuid.uuid4())
    q: asyncio.Queue = asyncio.Queue()
    _sse_sessions[sid] = q

    resp = web.StreamResponse()
    resp.headers.update({
        "Content-Type":                "text/event-stream",
        "Cache-Control":               "no-cache",
        "Connection":                  "keep-alive",
        "Access-Control-Allow-Origin": "*",
    })
    await resp.prepare(request)
    await resp.write(f"event: endpoint\ndata: /message?session_id={sid}\n\n".encode())

    try:
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=15)
                await resp.write(f"data: {json.dumps(msg)}\n\n".encode())
            except asyncio.TimeoutError:
                await resp.write(b": ping\n\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        _sse_sessions.pop(sid, None)
    return resp


async def handle_message(request: web.Request):
    """Legacy SSE POST endpoint."""
    sid = request.rel_url.query.get("session_id")
    q   = _sse_sessions.get(sid)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    result = await dispatch_rpc(body)
    if q:
        await q.put(result)
    else:
        return web.json_response(result)
    return web.Response(status=202)


async def handle_mcp_post(request: web.Request):
    """Streamable HTTP transport — POST /mcp."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response(_err(None, -32700, "Parse error"), status=400)

    accept   = request.headers.get("Accept", "application/json")
    is_batch = isinstance(body, list)
    items    = body if is_batch else [body]

    responses = [await dispatch_rpc(item) for item in items]
    result_body = responses if is_batch else (responses[0] if responses else None)

    if "text/event-stream" in accept:
        sr = web.StreamResponse()
        sr.headers.update({
            "Content-Type":                "text/event-stream",
            "Cache-Control":               "no-cache",
            "Access-Control-Allow-Origin": "*",
        })
        await sr.prepare(request)
        if result_body:
            await sr.write(f"data: {json.dumps(result_body)}\n\n".encode())
        return sr

    if result_body is None:
        return web.Response(status=202)
    return web.json_response(result_body, headers={"Access-Control-Allow-Origin": "*"})


async def handle_mcp_get(request: web.Request):
    """Streamable HTTP SSE channel — GET /mcp."""
    sid = str(uuid.uuid4())
    q: asyncio.Queue = asyncio.Queue()
    _mcp_sessions[sid] = q

    resp = web.StreamResponse()
    resp.headers.update({
        "Content-Type":                "text/event-stream",
        "Cache-Control":               "no-cache",
        "Access-Control-Allow-Origin": "*",
        "Mcp-Session-Id":              sid,
    })
    await resp.prepare(request)
    await resp.write(f"event: session\ndata: {json.dumps({'session_id': sid})}\n\n".encode())

    try:
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=15)
                await resp.write(f"data: {json.dumps(msg)}\n\n".encode())
            except asyncio.TimeoutError:
                await resp.write(b": ping\n\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        _mcp_sessions.pop(sid, None)
    return resp


async def handle_mcp_delete(request: web.Request):
    sid = request.headers.get("Mcp-Session-Id", "")
    _mcp_sessions.pop(sid, None)
    return web.json_response({"deleted": True})


async def handle_options(request: web.Request):
    return web.Response(headers={
        "Access-Control-Allow-Origin":  "*",
        "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization, Accept, Mcp-Session-Id",
    })


# ══════════════════════════════════════════════════════════════════
#  BACKGROUND CACHE REFRESH
# ══════════════════════════════════════════════════════════════════

async def cache_refresh_loop():
    """Periodically refresh the tool cache from all backends."""
    while True:
        await asyncio.sleep(CONFIG["TOOL_CACHE_TTL"])
        try:
            await refresh_tools()
        except Exception as exc:
            log.error(f"Background cache refresh failed: {exc}")


async def on_startup(app):
    """Load tools on startup — retry up to 6 times (30 s each)."""
    log.info("Loading tool cache from backends...")
    for attempt in range(6):
        await refresh_tools()
        if _tool_cache:
            break
        log.warning(f"No tools yet (attempt {attempt + 1}/6) — retrying in 5 s")
        await asyncio.sleep(5)
    asyncio.create_task(cache_refresh_loop())


# ══════════════════════════════════════════════════════════════════
#  APP FACTORY & MAIN
# ══════════════════════════════════════════════════════════════════

def create_app():
    app = web.Application(middlewares=[auth_middleware])
    app.on_startup.append(on_startup)
    app.router.add_get   ("/",        handle_health)
    app.router.add_get   ("/health",  handle_health)
    app.router.add_get   ("/metrics", handle_metrics)
    app.router.add_get   ("/sse",     handle_sse)
    app.router.add_post  ("/message", handle_message)
    app.router.add_post  ("/mcp",     handle_mcp_post)
    app.router.add_get   ("/mcp",     handle_mcp_get)
    app.router.add_delete("/mcp",     handle_mcp_delete)
    app.router.add_route ("OPTIONS", "/{path_info:.*}", handle_options)
    return app


async def main():
    app    = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, CONFIG["HOST"], CONFIG["PORT"])
    await site.start()

    p = CONFIG["PORT"]
    h = CONFIG["HOST"]
    print("╔══════════════════════════════════════════════════════════════════╗")
    print("║              MCP Gateway  —  Running                            ║")
    print("╠══════════════════════════════════════════════════════════════════╣")
    print(f"║  Streamable HTTP  →  POST  http://{h}:{p}/mcp")
    print(f"║  SSE (legacy)     →  GET   http://{h}:{p}/sse")
    print(f"║  Health           →  GET   http://{h}:{p}/health")
    print(f"║  Metrics          →  GET   http://{h}:{p}/metrics")
    print("╠══════════════════════════════════════════════════════════════════╣")
    for name, b in BACKENDS.items():
        print(f"║  {b['color']} {name:10} → {b['url']}  ({b['prefix']}*)")
    print("╚══════════════════════════════════════════════════════════════════╝")

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
