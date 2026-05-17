
#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║           Jenkins MCP Server  —  Full Production Build          ║
║                                                                  ║
║  • 52 tools covering every major Jenkins operation               ║
║  • Pure aiohttp: SSE transport + Streamable HTTP transport       ║
║  • JSON-RPC 2.0  (MCP protocol 2024-11-05)                      ║
║  • Bearer token auth                                             ║
║  • Docker-ready: all config via ENV vars                         ║
║                                                                  ║
║  Dependencies:  pip install aiohttp                              ║
║  Docker:        docker build -t jenkins-mcp .                    ║
║                 docker run -p 6500:6500 --env-file .env          ║
║                   jenkins-mcp                                    ║
╚══════════════════════════════════════════════════════════════════╝
"""

import asyncio
import json
import os
import base64
import uuid
import ssl
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urljoin, urlencode
from urllib.request import urlopen, Request
from urllib.error import HTTPError

from aiohttp import web

# ══════════════════════════════════════════════════════════════════
#  CONFIGURATION  ──  Edit values here, or set env vars to override
# ══════════════════════════════════════════════════════════════════

# All configuration is read exclusively from environment variables.
# Set them in the Dockerfile, docker run -e, or --env-file.
# No hardcoded values — the server will not start without JENKINS_TOKEN.
CONFIG = {
    "JENKINS_URL":        os.environ.get("JENKINS_URL",        ""),
    "JENKINS_USERNAME":   os.environ.get("JENKINS_USERNAME",   "admin"),
    "JENKINS_TOKEN":      os.environ.get("JENKINS_TOKEN",      ""),
    "JENKINS_VERIFY_SSL": os.environ.get("JENKINS_VERIFY_SSL", "true"),
    "HOST":               os.environ.get("HOST",               "0.0.0.0"),
    "PORT":               os.environ.get("PORT",               "6500"),
    "MCP_SECRET_TOKEN":   os.environ.get("MCP_SECRET_TOKEN",   ""),
}

# Fail fast if required variables are missing
_missing = [k for k in ("JENKINS_URL", "JENKINS_TOKEN") if not CONFIG[k]]
if _missing:
    import sys
    print(f"ERROR: Required environment variables not set: {', '.join(_missing)}")
    print("Set them via Docker: -e JENKINS_URL=... -e JENKINS_TOKEN=...")
    sys.exit(1)

# ══════════════════════════════════════════════════════════════════
#  JENKINS CLIENT
# ══════════════════════════════════════════════════════════════════

class JenkinsClient:
    def __init__(self, url: str, username: str, token: str, verify_ssl: bool = True):
        self.base_url  = url.rstrip("/")
        self.verify_ssl = verify_ssl
        creds = f"{username}:{token}"
        self._auth = base64.b64encode(creds.encode()).decode()

    def _ctx(self):
        if not self.verify_ssl:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode    = ssl.CERT_NONE
            return ctx
        return None

    def _req(self, path: str, method: str = "GET", data: Optional[bytes] = None,
             content_type: str = "application/json", extra: dict = None) -> Any:
        url  = urljoin(self.base_url + "/", path.lstrip("/"))
        hdrs = {"Authorization": f"Basic {self._auth}", "Content-Type": content_type}
        if extra:
            hdrs.update(extra)
        req = Request(url, data=data, headers=hdrs, method=method)
        try:
            with urlopen(req, context=self._ctx(), timeout=30) as r:
                body = r.read()
                ct   = r.headers.get("Content-Type", "")
                if "json" in ct:
                    return json.loads(body) if body else {}
                return body.decode("utf-8", errors="replace") if body else ""
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code} {e.reason}: {body[:500]}")

    def get(self, path):                                      return self._req(path)
    def post(self, path, data=b"", ct="application/x-www-form-urlencoded", extra=None):
        return self._req(path, "POST", data, ct, extra)
    def delete(self, path):                                   return self._req(path, "DELETE")

    def crumb(self) -> dict:
        try:
            d = self.get("/crumbIssuer/api/json")
            if d and "crumbRequestField" in d:
                return {d["crumbRequestField"]: d["crumb"]}
        except Exception:
            pass
        return {}


def _client() -> JenkinsClient:
    url  = CONFIG["JENKINS_URL"]
    user = CONFIG["JENKINS_USERNAME"]
    tok  = CONFIG["JENKINS_TOKEN"]
    verify = CONFIG["JENKINS_VERIFY_SSL"].lower() != "false"
    if not url or not user or not tok:
        raise RuntimeError("JENKINS_URL, JENKINS_USERNAME, JENKINS_TOKEN must all be set in CONFIG.")
    return JenkinsClient(url, user, tok, verify)

# ══════════════════════════════════════════════════════════════════
#  RESULT HELPERS
# ══════════════════════════════════════════════════════════════════

@dataclass
class TC:                          # TextContent
    type: str = "text"
    text: str = ""

@dataclass
class CTR:                         # CallToolResult
    content: list = field(default_factory=list)
    isError: bool = False

@dataclass
class Tool:
    name:        str  = ""
    description: str  = ""
    inputSchema: dict = field(default_factory=dict)

def ok(data: Any) -> CTR:
    t = json.dumps(data, indent=2) if not isinstance(data, str) else data
    return CTR(content=[TC(text=t)])

def err(msg: str) -> CTR:
    return CTR(content=[TC(text=f"ERROR: {msg}")], isError=True)

# ══════════════════════════════════════════════════════════════════
#  TOOL REGISTRY  (52 tools)
# ══════════════════════════════════════════════════════════════════

def _t(name, desc, props=None, required=None):
    schema = {"type": "object", "properties": props or {}}
    if required:
        schema["required"] = required
    return Tool(name=name, description=desc, inputSchema=schema)

def _p(type_, desc):
    return {"type": type_, "description": desc}

ALL_TOOLS = [
    # ── SERVER ────────────────────────────────────────────────────
    _t("run_health_check",
       "Check Jenkins connectivity and auth. Returns version, mode, executor count, agents online/offline."),

    _t("server_info",
       "Get Jenkins server details: version, mode, executor count, security, primary view."),

    _t("server_stats",
       "Get overallLoad and executor usage statistics."),

    _t("list_plugins",
       "List all installed plugins with version, enabled status, and whether updates are available."),

    _t("install_plugin",
       "Install a Jenkins plugin by its short name (e.g. 'git', 'pipeline', 'docker').",
       {"plugin_id": _p("string", "Plugin short name")}, ["plugin_id"]),

    _t("server_quiet_down",
       "Put Jenkins into quiet-down mode — no new builds will start."),

    _t("cancel_quiet_down",
       "Cancel quiet-down mode and allow new builds again."),

    _t("restart",
       "Safely restart Jenkins — waits for running builds to complete first."),

    _t("reload_configuration",
       "Reload Jenkins configuration from disk without restarting."),

    # ── JOBS ──────────────────────────────────────────────────────
    _t("list_jobs",
       "List all jobs on the dashboard. Optionally filter by folder.",
       {"folder": _p("string", "Folder path (optional)")}),

    _t("get_job",
       "Get full details about a specific Jenkins job.",
       {"job_name": _p("string", "Job name")}, ["job_name"]),

    _t("create_job",
       "Create a new Jenkins job from an XML config string.",
       {"job_name": _p("string", "Job name"),
        "config_xml": _p("string", "Full Jenkins job XML config")},
       ["job_name", "config_xml"]),

    _t("copy_job",
       "Copy an existing job to a new name.",
       {"source_job": _p("string", "Source job name"),
        "new_job":    _p("string", "New job name")},
       ["source_job", "new_job"]),

    _t("delete_job",
       "Permanently delete a Jenkins job and all its build history.",
       {"job_name": _p("string", "Job name")}, ["job_name"]),

    _t("enable_job",
       "Enable a disabled Jenkins job.",
       {"job_name": _p("string", "Job name")}, ["job_name"]),

    _t("disable_job",
       "Disable a Jenkins job to prevent new builds from being triggered.",
       {"job_name": _p("string", "Job name")}, ["job_name"]),

    _t("get_job_config",
       "Get the raw XML configuration of a Jenkins job.",
       {"job_name": _p("string", "Job name")}, ["job_name"]),

    _t("update_job_config",
       "Push an updated XML configuration to an existing Jenkins job.",
       {"job_name":   _p("string", "Job name"),
        "config_xml": _p("string", "Updated XML config")},
       ["job_name", "config_xml"]),

    _t("rename_job",
       "Rename a Jenkins job. All build history is preserved.",
       {"job_name": _p("string", "Current job name"),
        "new_name": _p("string", "New job name")},
       ["job_name", "new_name"]),

    # ── BUILDS ────────────────────────────────────────────────────
    _t("build_job",
       "Trigger a build for a Jenkins job. Supports parameterized builds.",
       {"job_name":   _p("string", "Job name"),
        "parameters": _p("object", "Key-value build parameters (optional)")},
       ["job_name"]),

    _t("list_builds",
       "List recent builds for a job with number, result, duration, and timestamp.",
       {"job_name": _p("string", "Job name"),
        "count":    _p("integer", "Max builds to return (default 10)")},
       ["job_name"]),

    _t("get_build",
       "Get detailed information about a specific build.",
       {"job_name":     _p("string", "Job name"),
        "build_number": _p("integer", "Build number")},
       ["job_name", "build_number"]),

    _t("get_build_log",
       "Get the console log for a specific build. Use 'start' byte offset for large logs.",
       {"job_name":     _p("string",  "Job name"),
        "build_number": _p("integer", "Build number"),
        "start":        _p("integer", "Byte offset to start reading from (default 0)")},
       ["job_name", "build_number"]),

    _t("stop_build",
       "Abort a currently running Jenkins build.",
       {"job_name":     _p("string",  "Job name"),
        "build_number": _p("integer", "Build number")},
       ["job_name", "build_number"]),

    _t("delete_build",
       "Delete a specific build record from Jenkins.",
       {"job_name":     _p("string",  "Job name"),
        "build_number": _p("integer", "Build number")},
       ["job_name", "build_number"]),

    _t("get_last_build",
       "Get info about the most recent build of a job (any result).",
       {"job_name": _p("string", "Job name")}, ["job_name"]),

    _t("get_last_successful_build",
       "Get info about the most recent successful build of a job.",
       {"job_name": _p("string", "Job name")}, ["job_name"]),

    _t("get_build_test_results",
       "Get JUnit test results for a build: pass/fail counts and failure details.",
       {"job_name":     _p("string",  "Job name"),
        "build_number": _p("integer", "Build number")},
       ["job_name", "build_number"]),

    _t("get_build_artifacts",
       "List artifacts produced and archived by a specific build.",
       {"job_name":     _p("string",  "Job name"),
        "build_number": _p("integer", "Build number")},
       ["job_name", "build_number"]),

    _t("replay_pipeline",
       "Replay a pipeline build, optionally with an updated Groovy script.",
       {"job_name":      _p("string",  "Job name"),
        "build_number":  _p("integer", "Build number"),
        "groovy_script": _p("string",  "Updated Groovy script (optional)")},
       ["job_name", "build_number"]),

    # ── QUEUE ─────────────────────────────────────────────────────
    _t("list_queue",
       "List all builds currently waiting in the Jenkins build queue."),

    _t("cancel_queue_item",
       "Cancel a queued build item by its queue ID.",
       {"queue_id": _p("integer", "Queue item ID")}, ["queue_id"]),

    # ── NODES ─────────────────────────────────────────────────────
    _t("list_nodes",
       "List all Jenkins nodes/agents with online status and executor info."),

    _t("get_node",
       "Get detailed info about a specific Jenkins node/agent.",
       {"node_name": _p("string", "Node name. Use 'built-in' for the controller.")},
       ["node_name"]),

    _t("enable_node",
       "Bring a Jenkins node back online.",
       {"node_name": _p("string", "Node name")}, ["node_name"]),

    _t("disable_node",
       "Take a Jenkins node offline with an optional reason.",
       {"node_name": _p("string", "Node name"),
        "reason":    _p("string", "Reason for taking offline (optional)")},
       ["node_name"]),

    _t("delete_node",
       "Permanently delete a Jenkins agent node.",
       {"node_name": _p("string", "Node name")}, ["node_name"]),

    # ── VIEWS ─────────────────────────────────────────────────────
    _t("list_views",
       "List all Jenkins views (dashboard tabs)."),

    _t("get_view",
       "Get details and job list for a Jenkins view.",
       {"view_name": _p("string", "View name")}, ["view_name"]),

    _t("create_view",
       "Create a new Jenkins list view.",
       {"view_name":  _p("string", "View name"),
        "config_xml": _p("string", "View XML config (optional, defaults to ListView)")}),

    _t("delete_view",
       "Delete a Jenkins view. Jobs inside it are NOT deleted.",
       {"view_name": _p("string", "View name")}, ["view_name"]),

    _t("add_job_to_view",
       "Add an existing job to a Jenkins view.",
       {"view_name": _p("string", "View name"),
        "job_name":  _p("string", "Job name")},
       ["view_name", "job_name"]),

    _t("remove_job_from_view",
       "Remove a job from a Jenkins view without deleting the job.",
       {"view_name": _p("string", "View name"),
        "job_name":  _p("string", "Job name")},
       ["view_name", "job_name"]),

    # ── CREDENTIALS ───────────────────────────────────────────────
    _t("list_credentials",
       "List credentials in a Jenkins credentials store/domain.",
       {"store":  _p("string", "Store ID (default: system)"),
        "domain": _p("string", "Domain name (default: _ = global)")}),

    _t("create_credential",
       "Create a new username/password credential in Jenkins.",
       {"credential_id": _p("string", "Unique credential ID"),
        "username":      _p("string", "Username"),
        "password":      _p("string", "Password or token"),
        "description":   _p("string", "Description (optional)"),
        "store":         _p("string", "Store (default: system)"),
        "domain":        _p("string", "Domain (default: _)")},
       ["credential_id", "username", "password"]),

    _t("delete_credential",
       "Delete a credential from Jenkins by ID.",
       {"credential_id": _p("string", "Credential ID"),
        "store":         _p("string", "Store (default: system)"),
        "domain":        _p("string", "Domain (default: _)")},
       ["credential_id"]),

    # ── PIPELINE / GROOVY ─────────────────────────────────────────
    _t("run_groovy_script",
       "Execute a Groovy script on the Jenkins controller via Script Console.",
       {"script": _p("string", "Groovy script to execute")}, ["script"]),

    _t("validate_jenkinsfile",
       "Validate declarative Jenkinsfile syntax using Jenkins built-in linter.",
       {"jenkinsfile_content": _p("string", "Jenkinsfile content to validate")},
       ["jenkinsfile_content"]),

    _t("get_pipeline_steps",
       "Get stage/step breakdown for a pipeline build (Blue Ocean workflow API).",
       {"job_name":     _p("string",  "Job name"),
        "build_number": _p("integer", "Build number")},
       ["job_name", "build_number"]),

    # ── FOLDERS ───────────────────────────────────────────────────
    _t("create_folder",
       "Create a Jenkins folder to organise jobs. Requires CloudBees Folders plugin.",
       {"folder_name":   _p("string", "Folder name"),
        "parent_folder": _p("string", "Parent folder path (optional)")},
       ["folder_name"]),

    # ── SEARCH & SYSTEM ───────────────────────────────────────────
    _t("search_jobs",
       "Search all Jenkins jobs by name substring (case-insensitive).",
       {"query":       _p("string",  "Search string"),
        "max_results": _p("integer", "Max results to return (default 20)")},
       ["query"]),

    _t("get_system_log",
       "Retrieve recent Jenkins system log entries.",
       {"max_lines": _p("integer", "Max log lines (default 100)")}),
]

# ══════════════════════════════════════════════════════════════════
#  TOOL DISPATCHER
# ══════════════════════════════════════════════════════════════════

async def call_tool(name: str, args: dict) -> CTR:
    try:
        c = _client()
    except RuntimeError as e:
        return err(str(e))
    try:
        return await _dispatch(name, args, c)
    except Exception as e:
        return err(str(e))


async def _dispatch(name: str, a: dict, c: JenkinsClient) -> CTR:

    # ── SERVER ────────────────────────────────────────────────────
    if name == "run_health_check":
        info  = c.get("/api/json?tree=numExecutors,mode,url")
        nodes = c.get("/computer/api/json?tree=computer[displayName,offline]")
        return ok({
            "status":        "ok",
            "url":   info.get("url"),
            "mode":          info.get("mode"),
            "executors":     info.get("numExecutors"),
            "agents":        len(nodes.get("computer", [])),
            "agents_online": sum(1 for n in nodes.get("computer", []) if not n.get("offline")),
        })

    if name == "server_info":
        return ok(c.get("/api/json?tree=numExecutors,description,mode,url,useSecurity,"
                        "views[name],primaryView[name],slaveAgentPort,quietingDown"))

    if name == "server_stats":
        return ok(c.get("/overallLoad/api/json"))

    if name == "list_plugins":
        d = c.get("/pluginManager/api/json?depth=1&"
                  "tree=plugins[shortName,longName,version,enabled,active,hasUpdate]")
        return ok(d.get("plugins", d))

    if name == "install_plugin":
        pid = a["plugin_id"]
        xml = f'<jenkins><install plugin="{pid}@latest" /></jenkins>'
        c.post("/pluginManager/installNecessaryPlugins", xml.encode(), "text/xml", c.crumb())
        return ok(f"Plugin '{pid}' installation triggered.")

    if name == "server_quiet_down":
        c.post("/quietDown", extra=c.crumb())
        return ok("Jenkins is entering quiet-down mode.")

    if name == "cancel_quiet_down":
        c.post("/cancelQuietDown", extra=c.crumb())
        return ok("Quiet-down mode cancelled.")

    if name == "restart":
        c.post("/safeRestart", extra=c.crumb())
        return ok("Jenkins safe restart triggered.")

    if name == "reload_configuration":
        c.post("/reload", extra=c.crumb())
        return ok("Configuration reloaded from disk.")

    # ── JOBS ──────────────────────────────────────────────────────
    if name == "list_jobs":
        folder = a.get("folder", "")
        base   = f"/job/{folder}" if folder else ""
        d = c.get(f"{base}/api/json?tree=jobs[name,url,color,buildable,"
                  "description,lastBuild[number,result]]")
        return ok(d.get("jobs", d))

    if name == "get_job":
        return ok(c.get(f"/job/{a['job_name']}/api/json?depth=1"))

    if name == "create_job":
        c.post(f"/createItem?name={a['job_name']}", a["config_xml"].encode(),
               "text/xml", c.crumb())
        return ok(f"Job '{a['job_name']}' created.")

    if name == "copy_job":
        c.post(f"/createItem?name={a['new_job']}&mode=copy&from={a['source_job']}",
               extra=c.crumb())
        return ok(f"Job '{a['source_job']}' copied to '{a['new_job']}'.")

    if name == "delete_job":
        c.post(f"/job/{a['job_name']}/doDelete", extra=c.crumb())
        return ok(f"Job '{a['job_name']}' deleted.")

    if name == "enable_job":
        c.post(f"/job/{a['job_name']}/enable", extra=c.crumb())
        return ok(f"Job '{a['job_name']}' enabled.")

    if name == "disable_job":
        c.post(f"/job/{a['job_name']}/disable", extra=c.crumb())
        return ok(f"Job '{a['job_name']}' disabled.")

    if name == "get_job_config":
        return ok(c.get(f"/job/{a['job_name']}/config.xml"))

    if name == "update_job_config":
        c.post(f"/job/{a['job_name']}/config.xml", a["config_xml"].encode(),
               "text/xml", c.crumb())
        return ok(f"Job '{a['job_name']}' config updated.")

    if name == "rename_job":
        c.post(f"/job/{a['job_name']}/confirmRename?newName={a['new_name']}", extra=c.crumb())
        return ok(f"Job '{a['job_name']}' renamed to '{a['new_name']}'.")

    # ── BUILDS ────────────────────────────────────────────────────
    if name == "build_job":
        jn     = a["job_name"]
        params = a.get("parameters")
        crumb  = c.crumb()
        if params:
            c.post(f"/job/{jn}/buildWithParameters",
                   urlencode(params).encode(), extra=crumb)
        else:
            c.post(f"/job/{jn}/build", extra=crumb)
        return ok(f"Build triggered for '{jn}'.")

    if name == "list_builds":
        jn    = a["job_name"]
        count = a.get("count", 10)
        d = c.get(f"/job/{jn}/api/json?tree=builds[number,result,duration,"
                  f"timestamp,displayName,building,url]{{0,{count}}}")
        return ok(d.get("builds", d))

    if name == "get_build":
        jn, bn = a["job_name"], a["build_number"]
        return ok(c.get(f"/job/{jn}/{bn}/api/json?depth=1"))

    if name == "get_build_log":
        jn, bn  = a["job_name"], a["build_number"]
        start   = a.get("start", 0)
        return ok(c.get(f"/job/{jn}/{bn}/logText/progressiveText?start={start}"))

    if name == "stop_build":
        jn, bn = a["job_name"], a["build_number"]
        c.post(f"/job/{jn}/{bn}/stop", extra=c.crumb())
        return ok(f"Build #{bn} of '{jn}' stopped.")

    if name == "delete_build":
        jn, bn = a["job_name"], a["build_number"]
        c.post(f"/job/{jn}/{bn}/doDelete", extra=c.crumb())
        return ok(f"Build #{bn} of '{jn}' deleted.")

    if name == "get_last_build":
        return ok(c.get(f"/job/{a['job_name']}/lastBuild/api/json"))

    if name == "get_last_successful_build":
        return ok(c.get(f"/job/{a['job_name']}/lastSuccessfulBuild/api/json"))

    if name == "get_build_test_results":
        jn, bn = a["job_name"], a["build_number"]
        return ok(c.get(f"/job/{jn}/{bn}/testReport/api/json?depth=1"))

    if name == "get_build_artifacts":
        jn, bn = a["job_name"], a["build_number"]
        d = c.get(f"/job/{jn}/{bn}/api/json?tree=artifacts[*]")
        return ok(d.get("artifacts", d))

    if name == "replay_pipeline":
        jn, bn = a["job_name"], a["build_number"]
        script = a.get("groovy_script")
        crumb  = c.crumb()
        if script:
            c.post(f"/job/{jn}/{bn}/replay/run",
                   urlencode({"mainScript": script}).encode(), extra=crumb)
        else:
            c.post(f"/job/{jn}/{bn}/replay/run", extra=crumb)
        return ok(f"Pipeline replay triggered for '{jn}' build #{bn}.")

    # ── QUEUE ─────────────────────────────────────────────────────
    if name == "list_queue":
        d = c.get("/queue/api/json")
        return ok(d.get("items", d))

    if name == "cancel_queue_item":
        c.post(f"/queue/cancelItem?id={a['queue_id']}", extra=c.crumb())
        return ok(f"Queue item {a['queue_id']} cancelled.")

    # ── NODES ─────────────────────────────────────────────────────
    if name == "list_nodes":
        d = c.get("/computer/api/json?depth=1&tree=computer[displayName,offline,"
                  "temporarilyOffline,numExecutors,idle,description,offlineCause]")
        return ok(d.get("computer", d))

    if name == "get_node":
        return ok(c.get(f"/computer/{a['node_name']}/api/json?depth=1"))

    if name == "enable_node":
        c.post(f"/computer/{a['node_name']}/toggleOffline?offlineMessage=", extra=c.crumb())
        return ok(f"Node '{a['node_name']}' brought online.")

    if name == "disable_node":
        reason = a.get("reason", "Taken offline by MCP")
        c.post(f"/computer/{a['node_name']}/toggleOffline?offlineMessage={reason}",
               extra=c.crumb())
        return ok(f"Node '{a['node_name']}' taken offline: {reason}")

    if name == "delete_node":
        c.post(f"/computer/{a['node_name']}/doDelete", extra=c.crumb())
        return ok(f"Node '{a['node_name']}' deleted.")

    # ── VIEWS ─────────────────────────────────────────────────────
    if name == "list_views":
        d = c.get("/api/json?tree=views[name,url,description]")
        return ok(d.get("views", d))

    if name == "get_view":
        return ok(c.get(f"/view/{a['view_name']}/api/json?depth=1"))

    if name == "create_view":
        vn  = a["view_name"]
        xml = a.get("config_xml") or (
            f'<?xml version="1.0" encoding="UTF-8"?><hudson.model.ListView>'
            f'<name>{vn}</name><filterExecutors>false</filterExecutors>'
            f'<filterQueue>false</filterQueue>'
            f'<properties class="hudson.model.View$PropertyList"/>'
            f'<jobNames><comparator class="hudson.util.CaseInsensitiveComparator"/>'
            f'</jobNames><jobFilters/><columns/></hudson.model.ListView>'
        )
        c.post(f"/createView?name={vn}", xml.encode(), "text/xml", c.crumb())
        return ok(f"View '{vn}' created.")

    if name == "delete_view":
        c.post(f"/view/{a['view_name']}/doDelete", extra=c.crumb())
        return ok(f"View '{a['view_name']}' deleted.")

    if name == "add_job_to_view":
        c.post(f"/view/{a['view_name']}/addJobToView?name={a['job_name']}", extra=c.crumb())
        return ok(f"Job '{a['job_name']}' added to view '{a['view_name']}'.")

    if name == "remove_job_from_view":
        c.post(f"/view/{a['view_name']}/removeJobFromView?name={a['job_name']}", extra=c.crumb())
        return ok(f"Job '{a['job_name']}' removed from view '{a['view_name']}'.")

    # ── CREDENTIALS ───────────────────────────────────────────────
    if name == "list_credentials":
        store  = a.get("store", "system")
        domain = a.get("domain", "_")
        d = c.get(f"/credentials/store/{store}/domain/{domain}/api/json?depth=1")
        return ok(d.get("credentials", d))

    if name == "create_credential":
        store  = a.get("store", "system")
        domain = a.get("domain", "_")
        cid    = a["credential_id"]
        payload = {
            "": "0",
            "credentials": {
                "scope":       "GLOBAL",
                "id":          cid,
                "username":    a["username"],
                "password":    a["password"],
                "description": a.get("description", ""),
                "$class": ("com.cloudbees.plugins.credentials.impl"
                           ".UsernamePasswordCredentialsImpl"),
            },
        }
        body = urlencode({"json": json.dumps(payload)}).encode()
        c.post(f"/credentials/store/{store}/domain/{domain}/createCredentials",
               body, extra=c.crumb())
        return ok(f"Credential '{cid}' created.")

    if name == "delete_credential":
        store  = a.get("store", "system")
        domain = a.get("domain", "_")
        cid    = a["credential_id"]
        c.post(f"/credentials/store/{store}/domain/{domain}/credential/{cid}/doDelete",
               extra=c.crumb())
        return ok(f"Credential '{cid}' deleted.")

    # ── PIPELINE / GROOVY ─────────────────────────────────────────
    if name == "run_groovy_script":
        body   = urlencode({"script": a["script"]}).encode()
        result = c.post("/scriptText", body, extra=c.crumb())
        return ok(result)

    if name == "validate_jenkinsfile":
        body   = urlencode({"jenkinsfile": a["jenkinsfile_content"]}).encode()
        result = c.post("/pipeline-model-converter/validate", body, extra=c.crumb())
        return ok(result)

    if name == "get_pipeline_steps":
        jn, bn = a["job_name"], a["build_number"]
        return ok(c.get(f"/job/{jn}/{bn}/wfapi/describe"))

    # ── FOLDERS ───────────────────────────────────────────────────
    if name == "create_folder":
        fn     = a["folder_name"]
        parent = a.get("parent_folder", "")
        base   = f"/job/{parent}" if parent else ""
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<com.cloudbees.hudson.plugins.folder.Folder plugin="cloudbees-folder">'
            f'<description>{fn} folder</description>'
            '</com.cloudbees.hudson.plugins.folder.Folder>'
        )
        c.post(f"{base}/createItem?name={fn}", xml.encode(), "text/xml", c.crumb())
        return ok(f"Folder '{fn}' created.")

    # ── SEARCH & SYSTEM ───────────────────────────────────────────
    if name == "search_jobs":
        query = a["query"].lower()
        max_r = a.get("max_results", 20)
        d     = c.get("/api/json?tree=jobs[name,url,color,buildable]&depth=1")
        jobs  = d.get("jobs", [])
        return ok([j for j in jobs if query in j.get("name", "").lower()][:max_r])

    if name == "get_system_log":
        max_lines = a.get("max_lines", 100)
        data  = c.get("/log/rss?all")
        lines = data.splitlines()[-max_lines:]
        return ok("\n".join(lines))

    return err(f"Unknown tool: {name}")


# ══════════════════════════════════════════════════════════════════
#  JSON-RPC 2.0 HELPERS
# ══════════════════════════════════════════════════════════════════

def _rpc_ok(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}

def _rpc_err(req_id, code, msg):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": msg}}

def _tool_dict(t: Tool) -> dict:
    return {"name": t.name, "description": t.description, "inputSchema": t.inputSchema}

def _ctr_dict(r: CTR) -> dict:
    return {"content": [{"type": c.type, "text": c.text} for c in r.content],
            "isError": r.isError}

# ══════════════════════════════════════════════════════════════════
#  SSE SESSION STORE
# ══════════════════════════════════════════════════════════════════

_sessions: dict[str, asyncio.Queue] = {}

# ══════════════════════════════════════════════════════════════════
#  AUTH MIDDLEWARE
# ══════════════════════════════════════════════════════════════════

@web.middleware
async def auth_middleware(request: web.Request, handler):
    # Health check is always public
    if request.path in ("/health", "/"):
        return await handler(request)

    token = CONFIG.get("MCP_SECRET_TOKEN", "").strip()
    if token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header != f"Bearer {token}":
            return web.json_response(
                {"error": "Unauthorized — provide Authorization: Bearer <token>"},
                status=401,
            )
    return await handler(request)

# ══════════════════════════════════════════════════════════════════
#  HTTP HANDLERS
# ══════════════════════════════════════════════════════════════════

async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({
        "status":      "ok",
        "server":      "jenkins-mcp",
        "tools":       len(ALL_TOOLS),
        "url": CONFIG["JENKINS_URL"],
        "auth":        bool(CONFIG.get("MCP_SECRET_TOKEN", "").strip()),
    })


async def handle_sse(request: web.Request) -> web.StreamResponse:
    """Long-lived SSE stream. Sends keepalive ping every 15 s to keep connection alive."""
    session_id = str(uuid.uuid4())
    q: asyncio.Queue = asyncio.Queue()
    _sessions[session_id] = q

    resp = web.StreamResponse()
    resp.headers.update({
        "Content-Type":                "text/event-stream",
        "Cache-Control":               "no-cache",
        "Connection":                  "keep-alive",
        "Access-Control-Allow-Origin": "*",
        "X-Session-Id":                session_id,
    })
    await resp.prepare(request)

    # Endpoint URL — relative path works for both direct and proxied deployments
    endpoint = f"/message?session_id={session_id}"

    await resp.write(f"event: endpoint\ndata: {endpoint}\n\n".encode())

    try:
        while True:
            try:
                msg  = await asyncio.wait_for(q.get(), timeout=15)
                data = json.dumps(msg)
                await resp.write(f"data: {data}\n\n".encode())
            except asyncio.TimeoutError:
                await resp.write(b": ping\n\n")   # keepalive
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        _sessions.pop(session_id, None)

    return resp


async def handle_message(request: web.Request) -> web.Response:
    """Receive a JSON-RPC 2.0 message and route it. Pushes response back over SSE."""
    session_id = request.rel_url.query.get("session_id")
    q          = _sessions.get(session_id)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    req_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params", {})
    resp   = None

    if method == "initialize":
        resp = _rpc_ok(req_id, {
            "protocolVersion": "2025-03-26",
            "capabilities":    {"tools": {}},
            "serverInfo":      {"name": "jenkins-mcp", "version": "2.0.0"},
        })

    elif method == "notifications/initialized":
        return web.Response(status=202)

    elif method == "tools/list":
        resp = _rpc_ok(req_id, {"tools": [_tool_dict(t) for t in ALL_TOOLS]})

    elif method == "tools/call":
        result = await call_tool(params.get("name", ""), params.get("arguments", {}))
        resp   = _rpc_ok(req_id, _ctr_dict(result))

    elif method == "ping":
        resp = _rpc_ok(req_id, {})

    else:
        resp = _rpc_err(req_id, -32601, f"Method not found: {method}")

    if resp is not None:
        if q:
            await q.put(resp)
        else:
            return web.json_response(resp)   # inline response (no SSE session)

    return web.Response(status=202)


async def handle_options(request: web.Request) -> web.Response:
    """CORS pre-flight for all routes."""
    return web.Response(headers={
        "Access-Control-Allow-Origin":  "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization, Accept",
    })


# ══════════════════════════════════════════════════════════════════
#  STREAMABLE HTTP TRANSPORT  (MCP spec 2025-03-26)
#
#  Single endpoint  POST /mcp
#  ─────────────────────────────────────────────────────────────────
#  The client sends a JSON-RPC request/batch in the POST body.
#  The server inspects the Accept header:
#
#  • Accept: application/json
#      → simple request/response — return the JSON-RPC result
#        directly in the HTTP response body (200 OK).
#        Best for tools/list, tools/call, initialize, ping.
#
#  • Accept: text/event-stream   (or both)
#      → the server streams the response as SSE on the same
#        HTTP connection and keeps it open for server-initiated
#        messages (e.g. progress notifications).
#
#  GET /mcp  — open a long-lived SSE channel for server-push
#  DELETE /mcp?session_id=X  — close a session
# ══════════════════════════════════════════════════════════════════

# Streamable HTTP sessions: session_id -> asyncio.Queue
_mcp_sessions: dict[str, asyncio.Queue] = {}


async def _process_jsonrpc(body: dict) -> dict | None:
    """Dispatch a single JSON-RPC object. Returns response dict or None for notifications."""
    req_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params", {})

    if method == "initialize":
        return _rpc_ok(req_id, {
            "protocolVersion": "2025-03-26",
            "capabilities":    {"tools": {}},
            "serverInfo":      {"name": "jenkins-mcp", "version": "2.0.0"},
        })

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None  # notifications — no response

    if method == "tools/list":
        return _rpc_ok(req_id, {"tools": [_tool_dict(t) for t in ALL_TOOLS]})

    if method == "tools/call":
        result = await call_tool(params.get("name", ""), params.get("arguments", {}))
        return _rpc_ok(req_id, _ctr_dict(result))

    if method == "ping":
        return _rpc_ok(req_id, {})

    return _rpc_err(req_id, -32601, f"Method not found: {method}")


async def handle_mcp_post(request: web.Request) -> web.Response | web.StreamResponse:
    """
    POST /mcp  — Streamable HTTP transport entry point.

    Handles both:
      - Simple JSON response  (Accept: application/json)
      - SSE streaming response (Accept: text/event-stream)
    Also accepts an optional Mcp-Session-Id header to attach to an
    existing server-push session opened via GET /mcp.
    """
    # Parse body
    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            _rpc_err(None, -32700, "Parse error: invalid JSON"), status=400
        )

    accept      = request.headers.get("Accept", "application/json")
    session_id  = request.headers.get("Mcp-Session-Id", "")
    wants_stream = "text/event-stream" in accept

    # Handle JSON-RPC batch (list) or single object
    is_batch = isinstance(body, list)
    items    = body if is_batch else [body]

    # Process all items
    responses = []
    for item in items:
        resp = await _process_jsonrpc(item)
        if resp is not None:
            responses.append(resp)

    result_body = responses if is_batch else (responses[0] if responses else None)

    # ── Option 1: Client wants SSE streaming ─────────────────────
    if wants_stream:
        stream_resp = web.StreamResponse()
        stream_resp.headers.update({
            "Content-Type":                "text/event-stream",
            "Cache-Control":               "no-cache",
            "Connection":                  "keep-alive",
            "Access-Control-Allow-Origin": "*",
        })
        if session_id:
            stream_resp.headers["Mcp-Session-Id"] = session_id

        await stream_resp.prepare(request)

        # Send the response(s) as SSE event(s)
        if result_body is not None:
            data = json.dumps(result_body)
            await stream_resp.write(f"data: {data}\n\n".encode())

        # If there is an active server-push session, relay queued messages
        if session_id and session_id in _mcp_sessions:
            q = _mcp_sessions[session_id]
            try:
                while True:
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=30)
                        await stream_resp.write(
                            f"data: {json.dumps(msg)}\n\n".encode()
                        )
                    except asyncio.TimeoutError:
                        await stream_resp.write(b": ping\n\n")
            except (ConnectionResetError, asyncio.CancelledError):
                pass

        return stream_resp

    # ── Option 2: Plain JSON response ─────────────────────────────
    if result_body is None:
        return web.Response(status=202)  # notification accepted, no content

    return web.json_response(result_body)


async def handle_mcp_get(request: web.Request) -> web.StreamResponse:
    """
    GET /mcp  — Open a long-lived SSE channel for server-initiated messages.
    Returns a session_id in the Mcp-Session-Id response header.
    The client should include that header on subsequent POST /mcp calls
    to receive server-push notifications on this channel.
    """
    session_id = str(uuid.uuid4())
    q: asyncio.Queue = asyncio.Queue()
    _mcp_sessions[session_id] = q

    resp = web.StreamResponse()
    resp.headers.update({
        "Content-Type":                "text/event-stream",
        "Cache-Control":               "no-cache",
        "Connection":                  "keep-alive",
        "Access-Control-Allow-Origin": "*",
        "Mcp-Session-Id":              session_id,
    })
    await resp.prepare(request)

    # Send session-established event
    await resp.write(
        f"event: session\ndata: {json.dumps({'session_id': session_id})}\n\n".encode()
    )

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
        _mcp_sessions.pop(session_id, None)

    return resp


async def handle_mcp_delete(request: web.Request) -> web.Response:
    """DELETE /mcp?session_id=X  — Close a Streamable HTTP session."""
    session_id = request.rel_url.query.get("session_id", "")
    if session_id in _mcp_sessions:
        _mcp_sessions.pop(session_id, None)
        return web.Response(status=200, text="Session closed")
    return web.Response(status=404, text="Session not found")


# ══════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════

async def main():
    port = int(CONFIG["PORT"])
    host = CONFIG["HOST"]

    app = web.Application(middlewares=[auth_middleware])

    # ── Legacy SSE transport (MCP 2024-11-05) ─────────────────────
    app.router.add_get   ("/sse",     handle_sse)
    app.router.add_post  ("/message", handle_message)

    # ── Streamable HTTP transport (MCP 2025-03-26) ─────────────────
    app.router.add_post  ("/mcp",     handle_mcp_post)
    app.router.add_get   ("/mcp",     handle_mcp_get)
    app.router.add_route ("DELETE", "/mcp", handle_mcp_delete)

    # ── Utility ────────────────────────────────────────────────────
    app.router.add_get   ("/health",  handle_health)
    app.router.add_get   ("/",        handle_health)
    app.router.add_route ("OPTIONS", "/{path_info:.*}", handle_options)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    token_status = ("Bearer token ENABLED"
                    if CONFIG.get("MCP_SECRET_TOKEN", "").strip()
                    else "⚠️  No auth — set MCP_SECRET_TOKEN to secure")

    print("╔═══════════════════════════════════════════════════════════╗")
    print("║           Jenkins MCP Server  —  Running                  ║")
    print("╠═══════════════════════════════════════════════════════════╣")
    print(f"║  [Streamable HTTP]  POST   http://{host}:{port}/mcp")
    print(f"║  [Streamable HTTP]  GET    http://{host}:{port}/mcp  (server-push SSE)")
    print(f"║  [SSE legacy]       GET    http://{host}:{port}/sse")
    print(f"║  [SSE legacy]       POST   http://{host}:{port}/message")
    print(f"║  [Health]           GET    http://{host}:{port}/health")
    print(f"║  Tools       : {len(ALL_TOOLS)}")
    print(f"║  Jenkins URL : {CONFIG['JENKINS_URL']}")
    print(f"║  Jenkins user: {CONFIG['JENKINS_USERNAME']}")
    print(f"║  Auth        : {token_status}")
    print("╚═══════════════════════════════════════════════════════════╝")

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n👋 Shutting down Jenkins MCP Server...")
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
