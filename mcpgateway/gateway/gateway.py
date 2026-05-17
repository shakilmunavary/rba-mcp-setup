
#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║              MCP Gateway  —  Single Entry Point                  ║
║                                                                  ║
║  Routes:                                                         ║
║    /          → Browser dashboard UI                             ║
║    /health    → Health check JSON (no auth)                      ║
║    /metrics   → Prometheus metrics (no auth)                     ║
║    /mcp       → MCP Streamable HTTP transport                    ║
║    /sse       → MCP legacy SSE transport                         ║
║    /message   → MCP legacy SSE message endpoint                  ║
║                                                                  ║
║  Backends:                                                       ║
║    jenkins_*  → jenkins-mcp:6500                                ║
║    github_*   → github-mcp:6501                                 ║
║    gitlab_*   → gitlab-mcp:6502                                 ║
║    snow_*     → snow-mcp:6503                                   ║
║    sonar_*    → sonar-mcp:6504                                  ║
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

CONFIG = {
    "HOST":             os.environ.get("HOST",             "0.0.0.0"),
    "PORT":             int(os.environ.get("PORT",         "6000")),
    "MCP_SECRET_TOKEN": os.environ.get("MCP_SECRET_TOKEN", ""),
    "TOOL_CACHE_TTL":   int(os.environ.get("TOOL_CACHE_TTL", "300")),
}

BACKENDS: dict[str, dict] = {
    "jenkins": {"url": os.environ.get("JENKINS_MCP_URL", "http://jenkins-mcp:6500"),
                "token": os.environ.get("JENKINS_MCP_TOKEN", ""), "prefix": "jenkins_",
                "color": "#2E86C1", "label": "Jenkins", "port": 6500},
    "github":  {"url": os.environ.get("GITHUB_MCP_URL",  "http://github-mcp:6501"),
                "token": os.environ.get("GITHUB_MCP_TOKEN",  ""), "prefix": "github_",
                "color": "#24292E", "label": "GitHub", "port": 6501},
    "gitlab":  {"url": os.environ.get("GITLAB_MCP_URL",  "http://gitlab-mcp:6502"),
                "token": os.environ.get("GITLAB_MCP_TOKEN",  ""), "prefix": "gitlab_",
                "color": "#8E44AD", "label": "GitLab", "port": 6502},
    "snow":    {"url": os.environ.get("SNOW_MCP_URL",    "http://snow-mcp:6503"),
                "token": os.environ.get("SNOW_MCP_TOKEN",    ""), "prefix": "snow_",
                "color": "#1E8449", "label": "ServiceNow", "port": 6503},
    "sonar":   {"url": os.environ.get("SONAR_MCP_URL",   "http://sonar-mcp:6504"),
                "token": os.environ.get("SONAR_MCP_TOKEN",   ""), "prefix": "sonar_",
                "color": "#E74C3C", "label": "SonarQube", "port": 6504},
}

_tool_cache: list[dict] = []
_tool_cache_ts: float = 0.0
_tool_to_backend: dict[str, str] = {}
_backend_status: dict[str, str] = {}


async def _fetch_backend_tools(session, name, backend) -> list[dict]:
    url = backend["url"] + "/mcp"
    headers = {"Authorization": f"Bearer {backend['token']}",
               "Content-Type": "application/json", "Accept": "application/json"}
    try:
        async with session.post(
            url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers=headers, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            data = await resp.json(content_type=None)
            tools = data.get("result", {}).get("tools", [])
            _backend_status[name] = "ok"
            log.info(f"[{name}] fetched {len(tools)} tools")
            return tools
    except Exception as exc:
        _backend_status[name] = f"error: {exc}"
        log.warning(f"[{name}] fetch failed — {exc}")
        return []


async def refresh_tools() -> None:
    global _tool_cache, _tool_cache_ts, _tool_to_backend
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        results = await asyncio.gather(
            *[_fetch_backend_tools(session, n, b) for n, b in BACKENDS.items()],
            return_exceptions=True)
    tools, mapping = [], {}
    for (name, _), result in zip(BACKENDS.items(), results):
        if isinstance(result, list):
            for t in result:
                tools.append(t)
                mapping[t["name"]] = name
    _tool_cache = tools
    _tool_to_backend = mapping
    _tool_cache_ts = time.monotonic()
    log.info(f"Tool cache updated: {len(tools)} total tools")


async def get_tools() -> list[dict]:
    if not _tool_cache or (time.monotonic() - _tool_cache_ts) > CONFIG["TOOL_CACHE_TTL"]:
        await refresh_tools()
    return _tool_cache


def resolve_backend(tool_name: str) -> Optional[str]:
    if tool_name in _tool_to_backend:
        return _tool_to_backend[tool_name]
    for name, b in BACKENDS.items():
        if tool_name.startswith(b["prefix"]):
            return name
    return None


async def proxy_tool_call(backend_name: str, tool_name: str, arguments: dict) -> dict:
    b = BACKENDS[backend_name]
    headers = {"Authorization": f"Bearer {b['token']}",
               "Content-Type": "application/json", "Accept": "application/json"}
    payload = {"jsonrpc": "2.0", "id": str(uuid.uuid4()),
               "method": "tools/call", "params": {"name": tool_name, "arguments": arguments}}
    try:
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.post(
                b["url"] + "/mcp", json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                data = await resp.json(content_type=None)
                return data.get("result", {
                    "content": [{"type": "text", "text": "Empty response from backend"}],
                    "isError": True})
    except Exception as exc:
        log.error(f"Proxy error [{backend_name}/{tool_name}]: {exc}")
        return {"content": [{"type": "text", "text": f"Gateway proxy error: {exc}"}], "isError": True}


def _ok(rid, result):  return {"jsonrpc": "2.0", "id": rid, "result": result}
def _err(rid, c, msg): return {"jsonrpc": "2.0", "id": rid, "error": {"code": c, "message": msg}}


async def dispatch_rpc(body: dict) -> dict:
    method, params, rid = body.get("method", ""), body.get("params", {}), body.get("id")
    if method == "initialize":
        return _ok(rid, {"protocolVersion": "2025-03-26",
                         "capabilities": {"tools": {}},
                         "serverInfo": {"name": "mcp-gateway", "version": "1.0.0"}})
    if method in ("notifications/initialized", "notifications/cancelled", "ping"):
        return _ok(rid, {})
    if method == "tools/list":
        tools = await get_tools()
        cursor = params.get("cursor")
        start  = int(cursor) if cursor else 0
        chunk  = tools[start: start + 50]
        nxt    = str(start + 50) if start + 50 < len(tools) else None
        result = {"tools": chunk}
        if nxt:
            result["nextCursor"] = nxt
        return _ok(rid, result)
    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        bn = resolve_backend(tool_name)
        if not bn:
            await refresh_tools()
            bn = resolve_backend(tool_name)
        if not bn:
            return _ok(rid, {"content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}], "isError": True})
        log.info(f"Route  {tool_name}  →  {bn}")
        return _ok(rid, await proxy_tool_call(bn, tool_name, arguments))
    return _err(rid, -32601, f"Method not found: {method}")


@web.middleware
async def auth_middleware(request: web.Request, handler):
    public = {"/health", "/", "/metrics"}
    if request.path in public or request.method == "OPTIONS":
        return await handler(request)
    token = CONFIG["MCP_SECRET_TOKEN"].strip()
    if token and request.headers.get("Authorization", "") != f"Bearer {token}":
        return web.json_response({"error": "Unauthorized"}, status=401)
    return await handler(request)


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>MCP Gateway</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f4f5f7;color:#172b4d;min-height:100vh}
.topbar{background:#0d1117;padding:0 24px;height:56px;display:flex;align-items:center;gap:16px;position:sticky;top:0;z-index:50}
.logo{display:flex;align-items:center;gap:10px;color:#fff;font-weight:600;font-size:16px}
.logo svg{color:#58a6ff}
.total-pill{background:#21262d;color:#58a6ff;font-size:12px;padding:3px 10px;border-radius:20px;font-weight:500}
.spacer{flex:1}
.token-info{color:#8b949e;font-size:12px;font-family:monospace;background:#161b22;padding:4px 10px;border-radius:6px}
.filterbar{background:#fff;border-bottom:1px solid #e1e4e8;padding:12px 24px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;position:sticky;top:56px;z-index:40}
.search-wrap{position:relative;flex:1;max-width:320px}
.search-wrap svg{position:absolute;left:10px;top:50%;transform:translateY(-50%);color:#8b9cb8;pointer-events:none}
input[type=search]{width:100%;height:36px;padding:0 12px 0 34px;border:1px solid #d0d7de;border-radius:8px;font-size:13px;color:#172b4d;background:#f6f8fa;outline:none}
input[type=search]:focus{border-color:#0969da;background:#fff;box-shadow:0 0 0 3px rgba(9,105,218,.1)}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{height:30px;padding:0 14px;border:1.5px solid #d0d7de;border-radius:20px;font-size:12px;font-weight:500;cursor:pointer;background:#fff;color:#57606a;transition:all .15s;white-space:nowrap}
.chip:hover{border-color:#0969da;color:#0969da}
.chip.active{color:#fff;border-color:transparent}
.chip-jenkins.active{background:#1565c0}
.chip-github.active{background:#24292e}
.chip-gitlab.active{background:#6b2fa0}
.chip-snow.active{background:#1b5e20}
.chip-sonar.active{background:#b71c1c}
.chip-all.active{background:#0969da}
.stat-wrap{display:flex;gap:12px;margin-left:auto;flex-wrap:wrap}
.stat{font-size:12px;color:#57606a}
.stat b{color:#172b4d;font-weight:600}
.section-head{display:flex;align-items:center;gap:10px;padding:20px 24px 8px;font-size:13px;font-weight:600;color:#57606a;text-transform:uppercase;letter-spacing:.05em}
.section-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.section-count{font-size:11px;background:#f0f0f0;padding:2px 8px;border-radius:10px;color:#57606a;font-weight:400;text-transform:none;letter-spacing:0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px;padding:0 24px 24px}
.card{background:#fff;border:1px solid #e1e4e8;border-radius:10px;overflow:hidden;cursor:pointer;transition:box-shadow .15s,border-color .15s}
.card:hover{box-shadow:0 4px 16px rgba(0,0,0,.1);border-color:#c0c8d8}
.card-head{padding:12px 14px 8px;display:flex;align-items:center;gap:8px}
.cdot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.tool-name{font-size:13px;font-weight:600;font-family:monospace;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#0550ae}
.rcount{font-size:11px;color:#8b9cb8;white-space:nowrap}
.card-desc{padding:0 14px 10px;font-size:12px;color:#57606a;line-height:1.5;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:36px}
.card-foot{padding:8px 14px;border-top:1px solid #f0f0f0;display:flex;gap:5px;flex-wrap:wrap;min-height:34px}
.ptag{font-size:10px;padding:2px 7px;border-radius:5px;font-family:monospace}
.ptag.req{background:#dbeafe;color:#1e40af}
.ptag.opt{background:#f1f3f5;color:#57606a}
.summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;padding:20px 24px 8px}
.scard{background:#fff;border:1px solid #e1e4e8;border-radius:10px;padding:14px;text-align:center}
.scard .num{font-size:28px;font-weight:700;color:#172b4d}
.scard .dot-lbl{display:flex;align-items:center;justify-content:center;gap:6px;margin-top:6px;font-size:12px;font-weight:600}
.overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:200;align-items:flex-start;justify-content:center;padding:40px 16px;overflow-y:auto}
.overlay.open{display:flex}
.modal{background:#fff;border-radius:12px;width:100%;max-width:640px;overflow:hidden;border:1px solid #d0d7de;box-shadow:0 8px 32px rgba(0,0,0,.2)}
.modal-head{padding:18px 20px;border-bottom:1px solid #e1e4e8;display:flex;align-items:flex-start;gap:12px}
.modal-head .cdot{margin-top:5px;width:12px;height:12px}
.modal-title h2{font-size:15px;font-weight:600;font-family:monospace;word-break:break-all;color:#0550ae}
.modal-title p{font-size:13px;color:#57606a;margin-top:5px;line-height:1.5}
.close-x{background:none;border:none;cursor:pointer;font-size:20px;color:#8b9cb8;line-height:1;padding:2px}
.close-x:hover{color:#172b4d}
.modal-body{padding:18px 20px;display:flex;flex-direction:column;gap:16px}
.sec-label{font-size:11px;font-weight:600;color:#8b9cb8;text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px}
.prow{display:flex;align-items:baseline;gap:8px;padding:6px 0;border-bottom:1px solid #f0f0f0}
.prow:last-child{border:none}
.pkey{font-size:13px;font-family:monospace;font-weight:600;min-width:150px;color:#172b4d}
.ptype{font-size:11px;color:#8b9cb8;min-width:52px}
.pdesc{font-size:12px;color:#57606a;flex:1}
.star{color:#e24b4a;margin-right:2px}
.codebox{background:#0d1117;border-radius:8px;overflow:hidden}
.codebox-head{display:flex;align-items:center;justify-content:space-between;padding:8px 14px;border-bottom:1px solid #21262d}
.codebox-head span{font-size:11px;color:#8b949e;font-family:monospace}
.copy-btn{font-size:11px;background:#21262d;border:1px solid #30363d;color:#c9d1d9;padding:3px 10px;border-radius:5px;cursor:pointer}
.copy-btn:hover{background:#30363d}
.codebox pre{padding:14px;font-size:12px;font-family:monospace;color:#a8ff78;line-height:1.7;overflow-x:auto;white-space:pre;margin:0}
.loading{padding:80px 24px;text-align:center;color:#57606a}
.spinner{width:28px;height:28px;border:3px solid #e1e4e8;border-top-color:#0969da;border-radius:50%;animation:spin .7s linear infinite;margin:0 auto 14px}
@keyframes spin{to{transform:rotate(360deg)}}
.empty{padding:80px 24px;text-align:center;color:#57606a;font-size:14px}
.err-box{margin:20px 24px;padding:14px 16px;background:#fff8f8;border:1px solid #ffcccc;border-radius:8px;color:#c62828;font-size:13px}
</style>
</head>
<body>
<div class="topbar">
  <div class="logo">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
      <path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/>
    </svg>
    MCP Gateway
  </div>
  <span class="total-pill" id="total-pill">Loading...</span>
  <div class="spacer"></div>
  <span class="token-info" id="token-display"></span>
</div>
<div class="filterbar">
  <div class="search-wrap">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
      <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
    </svg>
    <input type="search" id="search" placeholder="Search tools by name or description..." oninput="render()"/>
  </div>
  <div class="chips">
    <button class="chip chip-all active"  id="chip-all"     onclick="setFilter('all')">All</button>
    <button class="chip chip-jenkins"     id="chip-jenkins" onclick="setFilter('jenkins')">Jenkins</button>
    <button class="chip chip-github"      id="chip-github"  onclick="setFilter('github')">GitHub</button>
    <button class="chip chip-gitlab"      id="chip-gitlab"  onclick="setFilter('gitlab')">GitLab</button>
    <button class="chip chip-snow"        id="chip-snow"    onclick="setFilter('snow')">ServiceNow</button>
    <button class="chip chip-sonar"       id="chip-sonar"   onclick="setFilter('sonar')">SonarQube</button>
  </div>
  <div class="stat-wrap" id="stat-wrap"></div>
</div>
<div id="main"><div class="loading"><div class="spinner"></div>Connecting to gateway...</div></div>
<div class="overlay" id="overlay" onclick="if(event.target===this)closeModal()">
  <div class="modal" id="modal-box"></div>
</div>
<script>
const BACKENDS={
  jenkins:{color:"#1565c0",label:"Jenkins"},
  github: {color:"#24292e",label:"GitHub"},
  gitlab: {color:"#6b2fa0",label:"GitLab"},
  snow:   {color:"#1b5e20",label:"ServiceNow"},
  sonar:  {color:"#b71c1c",label:"SonarQube"},
};
function getBackend(name){
  for(const k of Object.keys(BACKENDS)) if(name.startsWith(k+'_')) return k;
  return 'other';
}
let allTools=[], filteredTools=[], activeFilter='all', TOKEN='';
async function loadTools(){
  try{
    TOKEN = document.querySelector('meta[name="mcp-token"]')?.content || '';
    document.getElementById('token-display').textContent = TOKEN ? 'Auth: Bearer ' + TOKEN : 'No auth';
    let tools=[], cursor=null;
    do{
      const body={jsonrpc:'2.0',id:1,method:'tools/list',params:cursor?{cursor}:{}};
      const headers={'Content-Type':'application/json'};
      if(TOKEN) headers['Authorization']='Bearer '+TOKEN;
      const resp=await fetch('/mcp',{method:'POST',headers,body:JSON.stringify(body)});
      if(!resp.ok) throw new Error('HTTP '+resp.status);
      const data=await resp.json();
      if(data.error) throw new Error(data.error.message);
      tools=tools.concat(data.result?.tools||[]);
      cursor=data.result?.nextCursor||null;
    } while(cursor);
    allTools=tools;
    document.getElementById('total-pill').textContent=tools.length+' tools';
    render();
  } catch(e){
    document.getElementById('main').innerHTML='<div class="err-box">Failed to load tools: '+e.message+'</div>';
  }
}
function setFilter(f){
  activeFilter=f;
  document.querySelectorAll('.chip').forEach(c=>c.classList.remove('active'));
  document.getElementById('chip-'+f).classList.add('active');
  render();
}
function render(){
  const q=(document.getElementById('search').value||'').toLowerCase();
  filteredTools=allTools.filter(t=>{
    const b=getBackend(t.name);
    if(activeFilter!=='all'&&b!==activeFilter) return false;
    if(q&&!t.name.toLowerCase().includes(q)&&!(t.description||'').toLowerCase().includes(q)) return false;
    return true;
  });
  const counts={};
  allTools.forEach(t=>{ const b=getBackend(t.name); counts[b]=(counts[b]||0)+1; });
  document.getElementById('stat-wrap').innerHTML=
    Object.entries(counts).map(([k,v])=>
      `<span class="stat"><b>${v}</b> ${BACKENDS[k]?.label||k}</span>`).join('');
  if(!filteredTools.length){
    document.getElementById('main').innerHTML='<div class="empty">No tools match your search.</div>';
    return;
  }
  const groups={};
  filteredTools.forEach((t,i)=>{ const b=getBackend(t.name); if(!groups[b]) groups[b]=[]; groups[b].push({t,i}); });
  const summaryHtml=activeFilter==='all'?
    `<div class="summary">${Object.entries(groups).map(([b,items])=>{
      const info=BACKENDS[b]||{color:'#888',label:b};
      return `<div class="scard"><div class="num">${items.length}</div>
        <div class="dot-lbl"><span style="width:8px;height:8px;border-radius:50%;background:${info.color};display:inline-block"></span>${info.label}</div>
      </div>`;
    }).join('')}</div>`:
    `<div style="padding:12px 24px 0;font-size:13px;color:#57606a"><b>${filteredTools.length}</b> tools in ${BACKENDS[activeFilter]?.label||activeFilter}</div>`;
  let body=summaryHtml;
  for(const [b,items] of Object.entries(groups)){
    const info=BACKENDS[b]||{color:'#888',label:b};
    body+=`<div class="section-head"><span class="section-dot" style="background:${info.color}"></span>${info.label}<span class="section-count">${items.length} tools</span></div><div class="grid">`;
    for(const {t,i} of items){
      const props=Object.entries(t.inputSchema?.properties||{});
      const req=t.inputSchema?.required||[];
      const tags=props.slice(0,5).map(([k])=>`<span class="ptag ${req.includes(k)?'req':'opt'}">${k}</span>`).join('');
      const more=props.length>5?`<span class="ptag opt">+${props.length-5}</span>`:'';
      body+=`<div class="card" onclick="openModal(${i})">
        <div class="card-head"><span class="cdot" style="background:${info.color}"></span>
          <span class="tool-name" title="${t.name}">${t.name}</span>
          <span class="rcount">${req.length}/${props.length}</span></div>
        <div class="card-desc">${t.description||'<i>No description</i>'}</div>
        ${props.length?`<div class="card-foot">${tags}${more}</div>`:'<div class="card-foot"></div>'}
      </div>`;
    }
    body+='</div>';
  }
  document.getElementById('main').innerHTML=body;
}
function openModal(idx){
  const t=filteredTools[idx];
  const b=getBackend(t.name);
  const info=BACKENDS[b]||{color:'#888',label:b};
  const props=Object.entries(t.inputSchema?.properties||{});
  const req=t.inputSchema?.required||[];
  const sampleArgs={};
  props.forEach(([k,v])=>{
    if(v.type==='string') sampleArgs[k]='your_'+k;
    else if(v.type==='integer') sampleArgs[k]=1;
    else if(v.type==='boolean') sampleArgs[k]=true;
    else if(v.type==='array') sampleArgs[k]=[];
    else if(v.type==='object') sampleArgs[k]={};
  });
  const origin=window.location.origin;
  const curl=`curl -sk -X POST ${origin}/mcp \\
${TOKEN?`  -H 'Authorization: Bearer ${TOKEN}' \\
`:''}  -H 'Content-Type: application/json' \\
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"${t.name}",
                 "arguments":${JSON.stringify(sampleArgs)}}}'`;
  const paramsHtml=props.length?props.map(([k,v])=>`
    <div class="prow">
      <span class="pkey">${req.includes(k)?'<span class="star">&#9733;</span>':''}<code>${k}</code></span>
      <span class="ptype">${v.type||''}</span>
      <span class="pdesc">${v.description||''}</span>
    </div>`).join('')
    :`<p style="font-size:13px;color:#57606a;padding:4px 0">No parameters required</p>`;
  document.getElementById('modal-box').innerHTML=`
    <div class="modal-head">
      <span class="cdot" style="background:${info.color}"></span>
      <div class="modal-title">
        <h2>${t.name}</h2>
        <p>${t.description||''}</p>
      </div>
      <button class="close-x" onclick="closeModal()" aria-label="Close">&times;</button>
    </div>
    <div class="modal-body">
      <div>
        <div class="sec-label">Parameters &nbsp;<span style="text-transform:none;letter-spacing:0;font-weight:400;color:#8b9cb8">&#9733; required &nbsp; no-star = optional</span></div>
        ${paramsHtml}
      </div>
      <div>
        <div class="sec-label">Sample curl command</div>
        <div class="codebox">
          <div class="codebox-head">
            <span>bash</span>
            <button class="copy-btn" onclick="doCopy(this)">Copy</button>
          </div>
          <pre id="curl-pre">${curl}</pre>
        </div>
      </div>
    </div>`;
  document.getElementById('overlay').classList.add('open');
  document.body.style.overflow='hidden';
}
function closeModal(){
  document.getElementById('overlay').classList.remove('open');
  document.body.style.overflow='';
}
function doCopy(btn){
  navigator.clipboard.writeText(document.getElementById('curl-pre').textContent)
    .then(()=>{ btn.textContent='Copied!'; setTimeout(()=>btn.textContent='Copy',1800); });
}
document.addEventListener('keydown',e=>{ if(e.key==='Escape') closeModal(); });
window.addEventListener('DOMContentLoaded', loadTools);
</script>
</body>
</html>"""


async def handle_dashboard(request: web.Request):
    token = CONFIG["MCP_SECRET_TOKEN"]
    html = DASHBOARD_HTML.replace(
        "</head>",
        f'<meta name="mcp-token" content="{token}"/></head>'
    )
    return web.Response(text=html, content_type="text/html")


async def handle_health(request: web.Request):
    connector = aiohttp.TCPConnector(ssl=False)
    bh = {}
    async with aiohttp.ClientSession(connector=connector) as session:
        for name, b in BACKENDS.items():
            try:
                async with session.get(b["url"] + "/health",
                                       timeout=aiohttp.ClientTimeout(total=5)) as r:
                    body = await r.json(content_type=None)
                    bh[name] = {"status": "ok" if r.status == 200 else f"HTTP {r.status}",
                                "tools": body.get("tools", 0), "url": b["url"]}
            except Exception as exc:
                bh[name] = {"status": f"error: {exc}", "tools": 0, "url": b["url"]}
    return web.json_response({
        "status": "ok", "server": "mcp-gateway", "port": CONFIG["PORT"],
        "total_tools": len(_tool_cache),
        "tool_counts": {n: sum(1 for t in _tool_cache if t["name"].startswith(BACKENDS[n]["prefix"]))
                        for n in BACKENDS},
        "backends": bh,
        "cache_age_s": round(time.monotonic() - _tool_cache_ts, 1),
    })


async def handle_metrics(request: web.Request):
    lines = [f"mcp_gateway_total_tools {len(_tool_cache)}"]
    for name in BACKENDS:
        c = sum(1 for t in _tool_cache if t["name"].startswith(BACKENDS[name]["prefix"]))
        lines.append(f'mcp_gateway_backend_tools{{backend="{name}"}} {c}')
    return web.Response(text="\n".join(lines) + "\n", content_type="text/plain")


_sse_sessions: dict[str, asyncio.Queue] = {}
_mcp_sessions: dict[str, asyncio.Queue] = {}


async def handle_sse(request: web.Request):
    sid = str(uuid.uuid4())
    q: asyncio.Queue = asyncio.Queue()
    _sse_sessions[sid] = q
    resp = web.StreamResponse()
    resp.headers.update({"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                         "Connection": "keep-alive", "Access-Control-Allow-Origin": "*"})
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
        sr.headers.update({"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                           "Access-Control-Allow-Origin": "*"})
        await sr.prepare(request)
        if result_body:
            await sr.write(f"data: {json.dumps(result_body)}\n\n".encode())
        return sr
    if result_body is None:
        return web.Response(status=202)
    return web.json_response(result_body, headers={"Access-Control-Allow-Origin": "*"})


async def handle_mcp_get(request: web.Request):
    sid = str(uuid.uuid4())
    q: asyncio.Queue = asyncio.Queue()
    _mcp_sessions[sid] = q
    resp = web.StreamResponse()
    resp.headers.update({"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                         "Access-Control-Allow-Origin": "*", "Mcp-Session-Id": sid})
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
    _mcp_sessions.pop(request.headers.get("Mcp-Session-Id", ""), None)
    return web.json_response({"deleted": True})


async def handle_options(request: web.Request):
    return web.Response(headers={
        "Access-Control-Allow-Origin":  "*",
        "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization, Accept, Mcp-Session-Id",
    })


async def cache_refresh_loop():
    while True:
        await asyncio.sleep(CONFIG["TOOL_CACHE_TTL"])
        try:
            await refresh_tools()
        except Exception as exc:
            log.error(f"Background refresh failed: {exc}")


async def on_startup(app):
    log.info("Loading tool cache from backends...")
    for attempt in range(6):
        await refresh_tools()
        if _tool_cache:
            break
        log.warning(f"No tools yet (attempt {attempt + 1}/6) — retrying in 5s")
        await asyncio.sleep(5)
    asyncio.create_task(cache_refresh_loop())


def create_app():
    app = web.Application(middlewares=[auth_middleware])
    app.on_startup.append(on_startup)
    app.router.add_get   ("/",        handle_dashboard)
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
    print("╔══════════════════════════════════════════════════════════════════╗")
    print("║              MCP Gateway  —  Running                            ║")
    print("╠══════════════════════════════════════════════════════════════════╣")
    print(f"║  Browser UI       →  GET  http://0.0.0.0:{p}/")
    print(f"║  MCP (Streamable) →  POST http://0.0.0.0:{p}/mcp")
    print(f"║  MCP (SSE legacy) →  GET  http://0.0.0.0:{p}/sse")
    print(f"║  Health           →  GET  http://0.0.0.0:{p}/health")
    print(f"║  Metrics          →  GET  http://0.0.0.0:{p}/metrics")
    print("╠══════════════════════════════════════════════════════════════════╣")
    for name, b in BACKENDS.items():
        print(f"║  {name:10} → {b['url']}")
    print("╚══════════════════════════════════════════════════════════════════╝")
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
