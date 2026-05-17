
#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║         SonarQube MCP Server  —  Full Production Build          ║
║                                                                  ║
║  • 60 tools covering every major SonarQube operation             ║
║  • Pure aiohttp: SSE transport + Streamable HTTP transport       ║
║  • JSON-RPC 2.0  (MCP protocol 2025-03-26)                      ║
║  • Bearer token auth                                             ║
║  • Docker-ready: all config via ENV vars                         ║
║                                                                  ║
║  Dependencies:  pip install aiohttp                              ║
╚══════════════════════════════════════════════════════════════════╝
"""

import asyncio
import json
import os
import uuid
import ssl
import base64
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urljoin, urlencode, quote
from urllib.request import urlopen, Request
from urllib.error import HTTPError

from aiohttp import web

# ══════════════════════════════════════════════════════════════════
#  CONFIGURATION
# ══════════════════════════════════════════════════════════════════

CONFIG = {
    "SONARQUBE_URL":      os.environ.get("SONARQUBE_URL",      "https://sop-testing-alb-2059918749.us-west-2.elb.amazonaws.com/sonarqube"),
    "SONAR_TOKEN":        os.environ.get("SONAR_TOKEN",        "sqa_647f0062f790b15ae6980f100aad098c60b8439d"),
    "SONAR_VERIFY_SSL":   os.environ.get("SONAR_VERIFY_SSL",   "false"),
    "HOST":               os.environ.get("HOST",               "0.0.0.0"),
    "PORT":               os.environ.get("PORT",               "6504"),
    "MCP_SECRET_TOKEN":   os.environ.get("MCP_SECRET_TOKEN",   "1234456789"),
}

_missing = [k for k in ("SONAR_TOKEN",) if not CONFIG[k]]
if _missing:
    import sys
    print(f"ERROR: Required environment variables not set: {', '.join(_missing)}")
    sys.exit(1)

# ══════════════════════════════════════════════════════════════════
#  SONARQUBE CLIENT
# ══════════════════════════════════════════════════════════════════

class SonarQubeClient:
    def __init__(self, base_url: str, token: str, verify_ssl: bool = True):
        self.api_base   = base_url.rstrip("/") + "/api"
        self.token      = token
        self.verify_ssl = verify_ssl
        # SonarQube uses token as username with empty password (Basic auth)
        creds = f"{token}:".encode()
        self._auth = base64.b64encode(creds).decode()

    def _ctx(self):
        if not self.verify_ssl:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode    = ssl.CERT_NONE
            return ctx
        return None

    def _req(self, path: str, method: str = "GET", params: Optional[dict] = None,
             data: Optional[dict] = None) -> Any:
        url = f"{self.api_base}/{path.lstrip('/')}"
        if params:
            url = url + "?" + urlencode({k: v for k, v in params.items() if v is not None})
        headers = {
            "Authorization": f"Basic {self._auth}",
            "Content-Type":  "application/x-www-form-urlencoded",
            "User-Agent":    "sonarqube-mcp-server/1.0",
        }
        body = None
        if data:
            body = urlencode({k: v for k, v in data.items() if v is not None}).encode()
        req = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(req, context=self._ctx(), timeout=30) as r:
                resp_body = r.read()
                ct = r.headers.get("Content-Type", "")
                if "json" in ct:
                    return json.loads(resp_body) if resp_body else {}
                return resp_body.decode("utf-8", errors="replace") if resp_body else ""
        except HTTPError as e:
            body_err = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code} {e.reason}: {body_err[:500]}")

    def get(self, path, params=None):           return self._req(path, "GET", params=params)
    def post(self, path, data=None):            return self._req(path, "POST", data=data)
    def delete(self, path, data=None):          return self._req(path, "DELETE", data=data)


def get_client() -> SonarQubeClient:
    verify = CONFIG["SONAR_VERIFY_SSL"].lower() != "false"
    return SonarQubeClient(CONFIG["SONARQUBE_URL"], CONFIG["SONAR_TOKEN"], verify)

# ══════════════════════════════════════════════════════════════════
#  TOOL REGISTRY
# ══════════════════════════════════════════════════════════════════

def ok(data: Any) -> dict:
    return {"success": True, "data": data}

def err(msg: str) -> dict:
    return {"success": False, "error": str(msg)}

ALL_TOOLS = []

def tool(name: str, desc: str, props: dict, required: list = None):
    ALL_TOOLS.append({
        "name": name,
        "description": desc,
        "inputSchema": {
            "type": "object",
            "properties": props,
            "required": required or [],
        },
    })

# ── SYSTEM / HEALTH ────────────────────────────────────────────────
tool("health_check", "Get SonarQube system health status", {})
tool("system_status", "Get SonarQube system status (UP, STARTING, etc)", {})
tool("system_info", "Get detailed system information and configuration", {})
tool("ping", "Simple liveness ping to SonarQube", {})
tool("server_version", "Get the SonarQube server version", {})

# ── PROJECTS ──────────────────────────────────────────────────────
tool("list_projects", "List all projects with optional filters", {
    "organization": {"type": "string", "description": "Organization key"},
    "query":        {"type": "string", "description": "Filter by name or key"},
    "page":         {"type": "integer", "description": "Page number (default 1)"},
    "page_size":    {"type": "integer", "description": "Results per page (default 50)"},
})
tool("get_project", "Get details of a specific project", {
    "project_key": {"type": "string", "description": "Project key"},
}, ["project_key"])
tool("create_project", "Create a new project", {
    "project_key":  {"type": "string", "description": "Unique project key"},
    "name":         {"type": "string", "description": "Project display name"},
    "visibility":   {"type": "string", "description": "public or private", "enum": ["public", "private"]},
    "main_branch":  {"type": "string", "description": "Main branch name (default: main)"},
}, ["project_key", "name"])
tool("delete_project", "Delete a project permanently", {
    "project_key": {"type": "string", "description": "Project key to delete"},
}, ["project_key"])
tool("update_project", "Update project name or visibility", {
    "project_key": {"type": "string", "description": "Project key"},
    "name":        {"type": "string", "description": "New project name"},
    "visibility":  {"type": "string", "description": "public or private"},
}, ["project_key"])
tool("get_project_tags", "Get tags applied to a project", {
    "project_key": {"type": "string", "description": "Project key"},
}, ["project_key"])
tool("set_project_tags", "Set tags on a project", {
    "project_key": {"type": "string", "description": "Project key"},
    "tags":        {"type": "string", "description": "Comma-separated list of tags"},
}, ["project_key", "tags"])
tool("search_projects", "Search projects with facets and filters", {
    "filter":    {"type": "string", "description": "Filter expression (e.g. reliability_rating=1)"},
    "facets":    {"type": "string", "description": "Comma-separated facets to return"},
    "sort":      {"type": "string", "description": "Sort field"},
    "asc":       {"type": "boolean", "description": "Ascending sort order"},
    "page":      {"type": "integer"},
    "page_size": {"type": "integer"},
})

# ── BRANCHES ──────────────────────────────────────────────────────
tool("list_branches", "List all branches for a project", {
    "project_key": {"type": "string", "description": "Project key"},
}, ["project_key"])
tool("get_branch", "Get details of a specific branch", {
    "project_key": {"type": "string", "description": "Project key"},
    "branch":      {"type": "string", "description": "Branch name"},
}, ["project_key", "branch"])
tool("delete_branch", "Delete a branch from a project", {
    "project_key": {"type": "string", "description": "Project key"},
    "branch":      {"type": "string", "description": "Branch name to delete"},
}, ["project_key", "branch"])
tool("rename_main_branch", "Rename the main branch of a project", {
    "project_key": {"type": "string", "description": "Project key"},
    "name":        {"type": "string", "description": "New main branch name"},
}, ["project_key", "name"])
tool("list_pull_requests", "List pull requests for a project", {
    "project_key": {"type": "string", "description": "Project key"},
}, ["project_key"])
tool("delete_pull_request", "Delete a pull request analysis", {
    "project_key":       {"type": "string", "description": "Project key"},
    "pull_request":      {"type": "string", "description": "Pull request ID"},
}, ["project_key", "pull_request"])

# ── MEASURES / METRICS ────────────────────────────────────────────
tool("get_measures", "Get measures (metrics) for a component", {
    "component":      {"type": "string", "description": "Component key (project, file, etc)"},
    "metric_keys":    {"type": "string", "description": "Comma-separated metric keys (e.g. coverage,bugs,code_smells)"},
    "branch":         {"type": "string", "description": "Branch name"},
    "pull_request":   {"type": "string", "description": "Pull request ID"},
}, ["component", "metric_keys"])
tool("get_measures_history", "Get historical measures for a project", {
    "component":   {"type": "string", "description": "Component key"},
    "metrics":     {"type": "string", "description": "Comma-separated metric keys"},
    "from":        {"type": "string", "description": "Start date YYYY-MM-DD"},
    "to":          {"type": "string", "description": "End date YYYY-MM-DD"},
    "page":        {"type": "integer"},
    "page_size":   {"type": "integer"},
}, ["component", "metrics"])
tool("list_metrics", "List all available metrics", {
    "page":      {"type": "integer"},
    "page_size": {"type": "integer"},
})
tool("component_tree", "Get measures for a component and its descendants", {
    "component":      {"type": "string", "description": "Base component key"},
    "metric_keys":    {"type": "string", "description": "Comma-separated metric keys"},
    "qualifiers":     {"type": "string", "description": "FIL, UTS, BRC, DIR, TRK"},
    "strategy":       {"type": "string", "description": "all, children, leaves"},
    "branch":         {"type": "string", "description": "Branch name"},
    "page":           {"type": "integer"},
    "page_size":      {"type": "integer"},
}, ["component", "metric_keys"])

# ── ISSUES ────────────────────────────────────────────────────────
tool("list_issues", "Search and list issues with filters", {
    "project_keys":      {"type": "string", "description": "Comma-separated project keys"},
    "types":             {"type": "string", "description": "BUG, VULNERABILITY, CODE_SMELL, SECURITY_HOTSPOT"},
    "severities":        {"type": "string", "description": "BLOCKER, CRITICAL, MAJOR, MINOR, INFO"},
    "statuses":          {"type": "string", "description": "OPEN, CONFIRMED, REOPENED, RESOLVED, CLOSED"},
    "resolutions":       {"type": "string", "description": "FALSE-POSITIVE, WONTFIX, FIXED, REMOVED"},
    "rules":             {"type": "string", "description": "Comma-separated rule keys"},
    "tags":              {"type": "string", "description": "Comma-separated tags"},
    "assignees":         {"type": "string", "description": "Comma-separated assignee logins"},
    "branch":            {"type": "string", "description": "Branch name"},
    "pull_request":      {"type": "string", "description": "Pull request ID"},
    "created_after":     {"type": "string", "description": "Date YYYY-MM-DD"},
    "created_before":    {"type": "string", "description": "Date YYYY-MM-DD"},
    "component_keys":    {"type": "string", "description": "Comma-separated component keys"},
    "page":              {"type": "integer"},
    "page_size":         {"type": "integer"},
})
tool("get_issue", "Get details of a specific issue", {
    "issue_key": {"type": "string", "description": "Issue key"},
}, ["issue_key"])
tool("assign_issue", "Assign an issue to a user", {
    "issue_key": {"type": "string", "description": "Issue key"},
    "assignee":  {"type": "string", "description": "Login of user to assign (leave empty to unassign)"},
}, ["issue_key"])
tool("set_issue_severity", "Change the severity of an issue", {
    "issue_key": {"type": "string", "description": "Issue key"},
    "severity":  {"type": "string", "description": "BLOCKER, CRITICAL, MAJOR, MINOR, INFO"},
}, ["issue_key", "severity"])
tool("set_issue_type", "Change the type of an issue", {
    "issue_key":   {"type": "string", "description": "Issue key"},
    "issue_type":  {"type": "string", "description": "BUG, VULNERABILITY, CODE_SMELL"},
}, ["issue_key", "issue_type"])
tool("do_transition", "Perform a workflow transition on an issue", {
    "issue_key":  {"type": "string", "description": "Issue key"},
    "transition": {"type": "string", "description": "confirm, unconfirm, reopen, resolve, falsepositive, wontfix, close"},
}, ["issue_key", "transition"])
tool("add_issue_comment", "Add a comment to an issue", {
    "issue_key": {"type": "string", "description": "Issue key"},
    "text":      {"type": "string", "description": "Comment text"},
}, ["issue_key", "text"])
tool("bulk_change_issues", "Apply bulk changes to multiple issues", {
    "issues":        {"type": "string", "description": "Comma-separated issue keys"},
    "assign":        {"type": "string", "description": "Assign to login"},
    "set_severity":  {"type": "string", "description": "New severity"},
    "set_type":      {"type": "string", "description": "New type"},
    "do_transition": {"type": "string", "description": "Transition to apply"},
    "add_tags":      {"type": "string", "description": "Comma-separated tags to add"},
    "remove_tags":   {"type": "string", "description": "Comma-separated tags to remove"},
}, ["issues"])

# ── QUALITY GATES ─────────────────────────────────────────────────
tool("list_quality_gates", "List all quality gates", {})
tool("get_quality_gate", "Get details of a quality gate", {
    "id":   {"type": "string", "description": "Quality gate ID"},
    "name": {"type": "string", "description": "Quality gate name"},
})
tool("get_project_quality_gate_status", "Get quality gate status for a project", {
    "project_key":   {"type": "string", "description": "Project key"},
    "branch":        {"type": "string", "description": "Branch name"},
    "pull_request":  {"type": "string", "description": "Pull request ID"},
}, ["project_key"])
tool("create_quality_gate", "Create a new quality gate", {
    "name": {"type": "string", "description": "Quality gate name"},
}, ["name"])
tool("add_quality_gate_condition", "Add a condition to a quality gate", {
    "gate_id":     {"type": "string", "description": "Quality gate ID"},
    "metric":      {"type": "string", "description": "Metric key"},
    "op":          {"type": "string", "description": "Operator: LT or GT"},
    "error":       {"type": "string", "description": "Error threshold value"},
}, ["gate_id", "metric", "op", "error"])
tool("associate_project_quality_gate", "Associate a project with a quality gate", {
    "project_key":     {"type": "string", "description": "Project key"},
    "gate_id":         {"type": "string", "description": "Quality gate ID"},
    "gate_name":       {"type": "string", "description": "Quality gate name"},
}, ["project_key"])

# ── QUALITY PROFILES ──────────────────────────────────────────────
tool("list_quality_profiles", "List all quality profiles", {
    "language":   {"type": "string", "description": "Filter by language"},
    "project":    {"type": "string", "description": "Filter by project key"},
})
tool("get_quality_profile", "Get details and settings of a quality profile", {
    "profile_key": {"type": "string", "description": "Quality profile key"},
}, ["profile_key"])
tool("associate_project_quality_profile", "Associate a project with a quality profile", {
    "project_key":    {"type": "string", "description": "Project key"},
    "language":       {"type": "string", "description": "Language"},
    "profile_name":   {"type": "string", "description": "Quality profile name"},
}, ["project_key", "language", "profile_name"])
tool("list_profile_rules", "List active rules in a quality profile", {
    "profile_key": {"type": "string", "description": "Quality profile key"},
    "page":        {"type": "integer"},
    "page_size":   {"type": "integer"},
}, ["profile_key"])

# ── RULES ─────────────────────────────────────────────────────────
tool("search_rules", "Search for rules with filters", {
    "query":        {"type": "string", "description": "Search text"},
    "languages":    {"type": "string", "description": "Comma-separated language keys"},
    "types":        {"type": "string", "description": "BUG, VULNERABILITY, CODE_SMELL"},
    "severities":   {"type": "string", "description": "BLOCKER, CRITICAL, MAJOR, MINOR, INFO"},
    "tags":         {"type": "string", "description": "Comma-separated tags"},
    "page":         {"type": "integer"},
    "page_size":    {"type": "integer"},
})
tool("get_rule", "Get detailed information about a rule", {
    "rule_key": {"type": "string", "description": "Rule key (e.g. java:S1234)"},
}, ["rule_key"])

# ── SECURITY HOTSPOTS ─────────────────────────────────────────────
tool("list_hotspots", "List security hotspots for a project", {
    "project_key":   {"type": "string", "description": "Project key"},
    "status":        {"type": "string", "description": "TO_REVIEW, REVIEWED"},
    "resolution":    {"type": "string", "description": "FIXED, SAFE, ACKNOWLEDGED"},
    "branch":        {"type": "string", "description": "Branch name"},
    "pull_request":  {"type": "string", "description": "Pull request ID"},
    "page":          {"type": "integer"},
    "page_size":     {"type": "integer"},
}, ["project_key"])
tool("get_hotspot", "Get details of a specific security hotspot", {
    "hotspot_key": {"type": "string", "description": "Hotspot key"},
}, ["hotspot_key"])
tool("update_hotspot_status", "Update the status of a security hotspot", {
    "hotspot_key": {"type": "string", "description": "Hotspot key"},
    "status":      {"type": "string", "description": "TO_REVIEW or REVIEWED"},
    "resolution":  {"type": "string", "description": "FIXED, SAFE, or ACKNOWLEDGED"},
}, ["hotspot_key", "status"])

# ── USERS & GROUPS ────────────────────────────────────────────────
tool("list_users", "List users with optional search filter", {
    "query":     {"type": "string", "description": "Search by login or name"},
    "page":      {"type": "integer"},
    "page_size": {"type": "integer"},
})
tool("get_user", "Get details of a specific user", {
    "login": {"type": "string", "description": "User login"},
}, ["login"])
tool("create_user", "Create a new local user", {
    "login":    {"type": "string", "description": "User login"},
    "name":     {"type": "string", "description": "Display name"},
    "email":    {"type": "string", "description": "Email address"},
    "password": {"type": "string", "description": "Password"},
}, ["login", "name", "password"])
tool("deactivate_user", "Deactivate a user account", {
    "login": {"type": "string", "description": "User login to deactivate"},
}, ["login"])
tool("list_groups", "List user groups", {
    "query":     {"type": "string", "description": "Search by name"},
    "page":      {"type": "integer"},
    "page_size": {"type": "integer"},
})
tool("create_group", "Create a new user group", {
    "name":        {"type": "string", "description": "Group name"},
    "description": {"type": "string", "description": "Group description"},
}, ["name"])
tool("add_user_to_group", "Add a user to a group", {
    "group_name": {"type": "string", "description": "Group name"},
    "login":      {"type": "string", "description": "User login"},
}, ["group_name", "login"])
tool("remove_user_from_group", "Remove a user from a group", {
    "group_name": {"type": "string", "description": "Group name"},
    "login":      {"type": "string", "description": "User login"},
}, ["group_name", "login"])

# ── PERMISSIONS ───────────────────────────────────────────────────
tool("list_project_permissions", "List permissions for a project", {
    "project_key": {"type": "string", "description": "Project key"},
    "page":        {"type": "integer"},
    "page_size":   {"type": "integer"},
}, ["project_key"])
tool("add_project_permission", "Grant a permission on a project to a user or group", {
    "project_key": {"type": "string", "description": "Project key"},
    "permission":  {"type": "string", "description": "admin, codeviewer, issueadmin, securityhotspotadmin, scan, user"},
    "login":       {"type": "string", "description": "User login (mutually exclusive with group_name)"},
    "group_name":  {"type": "string", "description": "Group name (mutually exclusive with login)"},
}, ["project_key", "permission"])
tool("remove_project_permission", "Revoke a permission on a project", {
    "project_key": {"type": "string", "description": "Project key"},
    "permission":  {"type": "string", "description": "Permission to revoke"},
    "login":       {"type": "string", "description": "User login"},
    "group_name":  {"type": "string", "description": "Group name"},
}, ["project_key", "permission"])

# ── TOKENS ────────────────────────────────────────────────────────
tool("list_tokens", "List user tokens", {
    "login": {"type": "string", "description": "User login (admin only for other users)"},
})
tool("generate_token", "Generate a new user token", {
    "name":            {"type": "string", "description": "Token name"},
    "login":           {"type": "string", "description": "User login (admin only for other users)"},
    "expiration_date": {"type": "string", "description": "Expiration date YYYY-MM-DD"},
}, ["name"])
tool("revoke_token", "Revoke a user token", {
    "name":  {"type": "string", "description": "Token name to revoke"},
    "login": {"type": "string", "description": "User login"},
}, ["name"])

# ── SOURCE CODE ───────────────────────────────────────────────────
tool("get_source", "Get annotated source code for a file", {
    "key":    {"type": "string", "description": "File component key"},
    "branch": {"type": "string", "description": "Branch name"},
    "from":   {"type": "integer", "description": "Start line number"},
    "to":     {"type": "integer", "description": "End line number"},
}, ["key"])
tool("get_scm_blame", "Get SCM blame info for a file", {
    "key":    {"type": "string", "description": "File component key"},
    "branch": {"type": "string", "description": "Branch name"},
    "from":   {"type": "integer", "description": "Start line number"},
    "to":     {"type": "integer", "description": "End line number"},
}, ["key"])

# ── NOTIFICATIONS & SETTINGS ──────────────────────────────────────
tool("list_settings", "List global or project settings", {
    "keys":        {"type": "string", "description": "Comma-separated setting keys"},
    "component":   {"type": "string", "description": "Project key for project-level settings"},
})
tool("set_setting", "Update a setting value", {
    "key":       {"type": "string", "description": "Setting key"},
    "value":     {"type": "string", "description": "New value"},
    "component": {"type": "string", "description": "Project key for project-level setting"},
}, ["key", "value"])
tool("reset_setting", "Reset a setting to its default value", {
    "keys":      {"type": "string", "description": "Comma-separated setting keys"},
    "component": {"type": "string", "description": "Project key for project-level setting"},
}, ["keys"])

# ── WEBHOOKS ──────────────────────────────────────────────────────
tool("list_webhooks", "List webhooks (global or project)", {
    "project_key": {"type": "string", "description": "Project key for project webhooks"},
})
tool("create_webhook", "Create a new webhook", {
    "name":        {"type": "string", "description": "Webhook name"},
    "url":         {"type": "string", "description": "Target URL"},
    "secret":      {"type": "string", "description": "Optional shared secret"},
    "project_key": {"type": "string", "description": "Project key (omit for global)"},
}, ["name", "url"])
tool("delete_webhook", "Delete a webhook", {
    "webhook_key": {"type": "string", "description": "Webhook key"},
}, ["webhook_key"])

# ══════════════════════════════════════════════════════════════════
#  TOOL DISPATCH
# ══════════════════════════════════════════════════════════════════

def dispatch(name: str, args: dict) -> Any:
    c = get_client()

    # ── SYSTEM ────────────────────────────────────────────────────
    if name == "health_check":
        return ok(c.get("system/health"))
    if name == "system_status":
        return ok(c.get("system/status"))
    if name == "system_info":
        return ok(c.get("system/info"))
    if name == "ping":
        c.get("system/ping"); return ok({"status": "alive"})
    if name == "server_version":
        return ok(c.get("server/version"))

    # ── PROJECTS ──────────────────────────────────────────────────
    if name == "list_projects":
        params = {}
        if args.get("organization"): params["organization"] = args["organization"]
        if args.get("query"):        params["q"]            = args["query"]
        params["p"]  = args.get("page", 1)
        params["ps"] = args.get("page_size", 50)
        return ok(c.get("projects/search", params))
    if name == "get_project":
        r = c.get("projects/search", {"projects": args["project_key"]})
        comps = r.get("components", [])
        return ok(comps[0] if comps else {})
    if name == "create_project":
        data = {"project": args["project_key"], "name": args["name"]}
        if args.get("visibility"):  data["visibility"]  = args["visibility"]
        if args.get("main_branch"): data["mainBranch"]  = args["main_branch"]
        return ok(c.post("projects/create", data))
    if name == "delete_project":
        return ok(c.post("projects/delete", {"project": args["project_key"]}))
    if name == "update_project":
        data = {"project": args["project_key"]}
        if args.get("name"):       data["name"]       = args["name"]
        if args.get("visibility"): data["visibility"] = args["visibility"]
        return ok(c.post("projects/update_visibility", data) or {"updated": True})
    if name == "get_project_tags":
        return ok(c.get("projects/search", {"projects": args["project_key"]}))
    if name == "set_project_tags":
        return ok(c.post("projects/set_tags", {"project": args["project_key"], "tags": args["tags"]}))
    if name == "search_projects":
        params = {}
        for k, pk in [("filter","filter"),("facets","facets"),("sort","s"),("page","p"),("page_size","ps")]:
            if args.get(k) is not None: params[pk] = args[k]
        if args.get("asc") is not None: params["asc"] = str(args["asc"]).lower()
        return ok(c.get("components/search_projects", params))

    # ── BRANCHES ──────────────────────────────────────────────────
    if name == "list_branches":
        return ok(c.get("project_branches/list", {"project": args["project_key"]}))
    if name == "get_branch":
        r = c.get("project_branches/list", {"project": args["project_key"]})
        for b in r.get("branches", []):
            if b["name"] == args["branch"]: return ok(b)
        return ok({})
    if name == "delete_branch":
        return ok(c.post("project_branches/delete", {"project": args["project_key"], "branch": args["branch"]}))
    if name == "rename_main_branch":
        return ok(c.post("project_branches/rename", {"project": args["project_key"], "name": args["name"]}))
    if name == "list_pull_requests":
        return ok(c.get("project_pull_requests/list", {"project": args["project_key"]}))
    if name == "delete_pull_request":
        return ok(c.post("project_pull_requests/delete", {"project": args["project_key"], "pullRequest": args["pull_request"]}))

    # ── MEASURES ──────────────────────────────────────────────────
    if name == "get_measures":
        params = {"component": args["component"], "metricKeys": args["metric_keys"]}
        if args.get("branch"):        params["branch"]      = args["branch"]
        if args.get("pull_request"):  params["pullRequest"] = args["pull_request"]
        return ok(c.get("measures/component", params))
    if name == "get_measures_history":
        params = {"component": args["component"], "metrics": args["metrics"]}
        for k, pk in [("from","from"),("to","to")]:
            if args.get(k): params[pk] = args[k]
        params["p"]  = args.get("page", 1)
        params["ps"] = args.get("page_size", 100)
        return ok(c.get("measures/search_history", params))
    if name == "list_metrics":
        params = {"p": args.get("page", 1), "ps": args.get("page_size", 100)}
        return ok(c.get("metrics/search", params))
    if name == "component_tree":
        params = {"component": args["component"], "metricKeys": args["metric_keys"]}
        for k, pk in [("qualifiers","qualifiers"),("strategy","strategy"),("branch","branch")]:
            if args.get(k): params[pk] = args[k]
        params["p"]  = args.get("page", 1)
        params["ps"] = args.get("page_size", 50)
        return ok(c.get("measures/component_tree", params))

    # ── ISSUES ────────────────────────────────────────────────────
    if name == "list_issues":
        params = {}
        for k, pk in [("project_keys","projectKeys"),("types","types"),("severities","severities"),
                      ("statuses","statuses"),("resolutions","resolutions"),("rules","rules"),
                      ("tags","tags"),("assignees","assignees"),("branch","branch"),
                      ("pull_request","pullRequest"),("created_after","createdAfter"),
                      ("created_before","createdBefore"),("component_keys","componentKeys")]:
            if args.get(k): params[pk] = args[k]
        params["p"]  = args.get("page", 1)
        params["ps"] = args.get("page_size", 50)
        return ok(c.get("issues/search", params))
    if name == "get_issue":
        return ok(c.get("issues/search", {"issues": args["issue_key"]}))
    if name == "assign_issue":
        data = {"issue": args["issue_key"]}
        if args.get("assignee"): data["assignee"] = args["assignee"]
        return ok(c.post("issues/assign", data))
    if name == "set_issue_severity":
        return ok(c.post("issues/set_severity", {"issue": args["issue_key"], "severity": args["severity"]}))
    if name == "set_issue_type":
        return ok(c.post("issues/set_type", {"issue": args["issue_key"], "type": args["issue_type"]}))
    if name == "do_transition":
        return ok(c.post("issues/do_transition", {"issue": args["issue_key"], "transition": args["transition"]}))
    if name == "add_issue_comment":
        return ok(c.post("issues/add_comment", {"issue": args["issue_key"], "text": args["text"]}))
    if name == "bulk_change_issues":
        data = {"issues": args["issues"]}
        for k, pk in [("assign","assign"),("set_severity","set_severity"),
                      ("set_type","set_type"),("do_transition","do_transition"),
                      ("add_tags","add_tags"),("remove_tags","remove_tags")]:
            if args.get(k): data[pk] = args[k]
        return ok(c.post("issues/bulk_change", data))

    # ── QUALITY GATES ─────────────────────────────────────────────
    if name == "list_quality_gates":
        return ok(c.get("qualitygates/list"))
    if name == "get_quality_gate":
        params = {}
        if args.get("id"):   params["id"]   = args["id"]
        if args.get("name"): params["name"] = args["name"]
        return ok(c.get("qualitygates/show", params))
    if name == "get_project_quality_gate_status":
        params = {"projectKey": args["project_key"]}
        if args.get("branch"):        params["branch"]      = args["branch"]
        if args.get("pull_request"):  params["pullRequest"] = args["pull_request"]
        return ok(c.get("qualitygates/project_status", params))
    if name == "create_quality_gate":
        return ok(c.post("qualitygates/create", {"name": args["name"]}))
    if name == "add_quality_gate_condition":
        return ok(c.post("qualitygates/create_condition", {
            "gateId": args["gate_id"], "metric": args["metric"],
            "op": args["op"], "error": args["error"]
        }))
    if name == "associate_project_quality_gate":
        data = {"projectKey": args["project_key"]}
        if args.get("gate_id"):   data["gateId"]   = args["gate_id"]
        if args.get("gate_name"): data["gateName"] = args["gate_name"]
        return ok(c.post("qualitygates/select", data))

    # ── QUALITY PROFILES ──────────────────────────────────────────
    if name == "list_quality_profiles":
        params = {}
        if args.get("language"): params["language"] = args["language"]
        if args.get("project"):  params["project"]  = args["project"]
        return ok(c.get("qualityprofiles/search", params))
    if name == "get_quality_profile":
        return ok(c.get("qualityprofiles/show", {"profile": args["profile_key"]}))
    if name == "associate_project_quality_profile":
        return ok(c.post("qualityprofiles/add_project", {
            "project": args["project_key"], "language": args["language"],
            "qualityProfile": args["profile_name"]
        }))
    if name == "list_profile_rules":
        params = {"activation": "true", "qprofile": args["profile_key"],
                  "p": args.get("page", 1), "ps": args.get("page_size", 100)}
        return ok(c.get("rules/search", params))

    # ── RULES ─────────────────────────────────────────────────────
    if name == "search_rules":
        params = {}
        for k, pk in [("query","q"),("languages","languages"),("types","types"),
                      ("severities","severities"),("tags","tags")]:
            if args.get(k): params[pk] = args[k]
        params["p"]  = args.get("page", 1)
        params["ps"] = args.get("page_size", 50)
        return ok(c.get("rules/search", params))
    if name == "get_rule":
        return ok(c.get("rules/show", {"key": args["rule_key"]}))

    # ── HOTSPOTS ──────────────────────────────────────────────────
    if name == "list_hotspots":
        params = {"projectKey": args["project_key"]}
        for k, pk in [("status","status"),("resolution","resolution"),
                      ("branch","branch"),("pull_request","pullRequest")]:
            if args.get(k): params[pk] = args[k]
        params["p"]  = args.get("page", 1)
        params["ps"] = args.get("page_size", 50)
        return ok(c.get("hotspots/search", params))
    if name == "get_hotspot":
        return ok(c.get("hotspots/show", {"hotspot": args["hotspot_key"]}))
    if name == "update_hotspot_status":
        data = {"hotspot": args["hotspot_key"], "status": args["status"]}
        if args.get("resolution"): data["resolution"] = args["resolution"]
        return ok(c.post("hotspots/change_status", data))

    # ── USERS & GROUPS ────────────────────────────────────────────
    if name == "list_users":
        params = {"p": args.get("page", 1), "ps": args.get("page_size", 50)}
        if args.get("query"): params["q"] = args["query"]
        return ok(c.get("users/search", params))
    if name == "get_user":
        return ok(c.get("users/search", {"q": args["login"]}))
    if name == "create_user":
        data = {"login": args["login"], "name": args["name"], "password": args["password"]}
        if args.get("email"): data["email"] = args["email"]
        return ok(c.post("users/create", data))
    if name == "deactivate_user":
        return ok(c.post("users/deactivate", {"login": args["login"]}))
    if name == "list_groups":
        params = {"p": args.get("page", 1), "ps": args.get("page_size", 50)}
        if args.get("query"): params["q"] = args["query"]
        return ok(c.get("user_groups/search", params))
    if name == "create_group":
        data = {"name": args["name"]}
        if args.get("description"): data["description"] = args["description"]
        return ok(c.post("user_groups/create", data))
    if name == "add_user_to_group":
        return ok(c.post("user_groups/add_user", {"name": args["group_name"], "login": args["login"]}))
    if name == "remove_user_from_group":
        return ok(c.post("user_groups/remove_user", {"name": args["group_name"], "login": args["login"]}))

    # ── PERMISSIONS ───────────────────────────────────────────────
    if name == "list_project_permissions":
        params = {"projectKey": args["project_key"],
                  "p": args.get("page", 1), "ps": args.get("page_size", 50)}
        return ok(c.get("permissions/users", params))
    if name == "add_project_permission":
        data = {"projectKey": args["project_key"], "permission": args["permission"]}
        if args.get("login"):      data["login"]     = args["login"]
        if args.get("group_name"): data["groupName"] = args["group_name"]
        endpoint = "permissions/add_user" if args.get("login") else "permissions/add_group"
        return ok(c.post(endpoint, data))
    if name == "remove_project_permission":
        data = {"projectKey": args["project_key"], "permission": args["permission"]}
        if args.get("login"):      data["login"]     = args["login"]
        if args.get("group_name"): data["groupName"] = args["group_name"]
        endpoint = "permissions/remove_user" if args.get("login") else "permissions/remove_group"
        return ok(c.post(endpoint, data))

    # ── TOKENS ────────────────────────────────────────────────────
    if name == "list_tokens":
        params = {}
        if args.get("login"): params["login"] = args["login"]
        return ok(c.get("user_tokens/search", params))
    if name == "generate_token":
        data = {"name": args["name"]}
        if args.get("login"):           data["login"]          = args["login"]
        if args.get("expiration_date"): data["expirationDate"] = args["expiration_date"]
        return ok(c.post("user_tokens/generate", data))
    if name == "revoke_token":
        data = {"name": args["name"]}
        if args.get("login"): data["login"] = args["login"]
        return ok(c.post("user_tokens/revoke", data))

    # ── SOURCE ────────────────────────────────────────────────────
    if name == "get_source":
        params = {"key": args["key"]}
        if args.get("branch"): params["branch"] = args["branch"]
        if args.get("from"):   params["from"]   = args["from"]
        if args.get("to"):     params["to"]     = args["to"]
        return ok(c.get("sources/show", params))
    if name == "get_scm_blame":
        params = {"key": args["key"]}
        if args.get("branch"): params["branch"] = args["branch"]
        if args.get("from"):   params["from"]   = args["from"]
        if args.get("to"):     params["to"]     = args["to"]
        return ok(c.get("sources/scm", params))

    # ── SETTINGS ──────────────────────────────────────────────────
    if name == "list_settings":
        params = {}
        if args.get("keys"):      params["keys"]      = args["keys"]
        if args.get("component"): params["component"] = args["component"]
        return ok(c.get("settings/values", params))
    if name == "set_setting":
        data = {"key": args["key"], "value": args["value"]}
        if args.get("component"): data["component"] = args["component"]
        return ok(c.post("settings/set", data))
    if name == "reset_setting":
        data = {"keys": args["keys"]}
        if args.get("component"): data["component"] = args["component"]
        return ok(c.post("settings/reset", data))

    # ── WEBHOOKS ──────────────────────────────────────────────────
    if name == "list_webhooks":
        params = {}
        if args.get("project_key"): params["project"] = args["project_key"]
        return ok(c.get("webhooks/list", params))
    if name == "create_webhook":
        data = {"name": args["name"], "url": args["url"]}
        if args.get("secret"):      data["secret"]  = args["secret"]
        if args.get("project_key"): data["project"] = args["project_key"]
        return ok(c.post("webhooks/create", data))
    if name == "delete_webhook":
        return ok(c.post("webhooks/delete", {"webhook": args["webhook_key"]}))

    return err(f"Unknown tool: {name}")

# ══════════════════════════════════════════════════════════════════
#  MCP JSON-RPC HANDLER
# ══════════════════════════════════════════════════════════════════

@dataclass
class Session:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)

_sessions:     dict[str, Session] = {}
_mcp_sessions: dict[str, Session] = {}


def _rpc_result(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}

def _rpc_error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


async def _handle_rpc(msg: dict) -> dict:
    method = msg.get("method", "")
    params = msg.get("params", {})
    req_id = msg.get("id")

    if method == "initialize":
        return _rpc_result(req_id, {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "sonarqube-mcp-server", "version": "1.0.0"},
        })

    if method == "tools/list":
        cursor = params.get("cursor")
        start  = int(cursor) if cursor else 0
        chunk  = ALL_TOOLS[start: start + 50]
        nxt    = str(start + 50) if start + 50 < len(ALL_TOOLS) else None
        return _rpc_result(req_id, {"tools": chunk, **({"nextCursor": nxt} if nxt else {})})

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        try:
            result = dispatch(tool_name, arguments)
            return _rpc_result(req_id, {
                "content": [{"type": "text", "text": json.dumps(result, indent=2)}]
            })
        except Exception as exc:
            return _rpc_result(req_id, {
                "content": [{"type": "text", "text": json.dumps(err(str(exc)), indent=2)}],
                "isError": True,
            })

    if method in ("notifications/initialized", "ping"):
        return _rpc_result(req_id, {})

    return _rpc_error(req_id, -32601, f"Method not found: {method}")

# ══════════════════════════════════════════════════════════════════
#  AUTH MIDDLEWARE
# ══════════════════════════════════════════════════════════════════

@web.middleware
async def auth_middleware(request: web.Request, handler):
    if request.path in ("/health", "/") or request.method == "OPTIONS":
        return await handler(request)
    token = CONFIG.get("MCP_SECRET_TOKEN", "").strip()
    if token:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {token}":
            return web.json_response({"error": "Unauthorized"}, status=401)
    return await handler(request)

# ══════════════════════════════════════════════════════════════════
#  HTTP HANDLERS
# ══════════════════════════════════════════════════════════════════

async def handle_health(request: web.Request):
    instance_name = CONFIG["SONARQUBE_URL"].split("//")[-1].split("/")[0]
    return web.json_response({
        "status": "ok",
        "server": "sonarqube-mcp",
        "tools":  len(ALL_TOOLS),
        "sonarqube_url": CONFIG["SONARQUBE_URL"],
    })

# ── LEGACY SSE TRANSPORT ──────────────────────────────────────────

async def handle_sse(request: web.Request):
    session = Session()
    _sessions[session.id] = session
    resp = web.StreamResponse(headers={
        "Content-Type":  "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection":    "keep-alive",
        "Access-Control-Allow-Origin": "*",
    })
    await resp.prepare(request)
    endpoint = f"/message?session_id={session.id}"
    await resp.write(f"event: endpoint\ndata: {json.dumps(endpoint)}\n\n".encode())
    try:
        while True:
            msg = await asyncio.wait_for(session.queue.get(), timeout=30)
            await resp.write(f"event: message\ndata: {json.dumps(msg)}\n\n".encode())
    except (asyncio.TimeoutError, ConnectionResetError):
        pass
    finally:
        _sessions.pop(session.id, None)
    return resp

async def handle_message(request: web.Request):
    sid = request.rel_url.query.get("session_id", "")
    session = _sessions.get(sid)
    if not session:
        return web.json_response({"error": "Session not found"}, status=404)
    body = await request.json()
    result = await _handle_rpc(body)
    await session.queue.put(result)
    return web.json_response({"status": "accepted"})

# ── STREAMABLE HTTP TRANSPORT (MCP 2025-03-26) ───────────────────

async def handle_mcp_post(request: web.Request):
    accept = request.headers.get("Accept", "")
    body   = await request.json()
    result = await _handle_rpc(body)

    if "text/event-stream" in accept:
        sid = str(uuid.uuid4())
        session = Session(id=sid)
        _mcp_sessions[sid] = session
        resp = web.StreamResponse(headers={
            "Content-Type":  "text/event-stream",
            "Cache-Control": "no-cache",
            "Mcp-Session-Id": sid,
            "Access-Control-Allow-Origin": "*",
        })
        await resp.prepare(request)
        await resp.write(f"event: message\ndata: {json.dumps(result)}\n\n".encode())
        await resp.write_eof()
        return resp

    headers = {"Access-Control-Allow-Origin": "*"}
    return web.json_response(result, headers=headers)

async def handle_mcp_get(request: web.Request):
    sid = request.headers.get("Mcp-Session-Id") or str(uuid.uuid4())
    session = _mcp_sessions.setdefault(sid, Session(id=sid))
    resp = web.StreamResponse(headers={
        "Content-Type":  "text/event-stream",
        "Cache-Control": "no-cache",
        "Mcp-Session-Id": sid,
        "Access-Control-Allow-Origin": "*",
    })
    await resp.prepare(request)
    try:
        while True:
            msg = await asyncio.wait_for(session.queue.get(), timeout=30)
            await resp.write(f"event: message\ndata: {json.dumps(msg)}\n\n".encode())
    except (asyncio.TimeoutError, ConnectionResetError):
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
#  APP FACTORY & MAIN
# ══════════════════════════════════════════════════════════════════

def create_app():
    app = web.Application(middlewares=[auth_middleware])
    app.router.add_get ("/health",  handle_health)
    app.router.add_get ("/",        handle_health)
    app.router.add_get ("/sse",     handle_sse)
    app.router.add_post("/message", handle_message)
    app.router.add_post("/mcp",     handle_mcp_post)
    app.router.add_get ("/mcp",     handle_mcp_get)
    app.router.add_delete("/mcp",   handle_mcp_delete)
    app.router.add_route("OPTIONS", "/{path_info:.*}", handle_options)
    return app

async def main():
    port = int(CONFIG["PORT"])
    host = CONFIG["HOST"]
    app  = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    print("╔══════════════════════════════════════════════════════════════════╗")
    print("║         SonarQube MCP Server  —  Running                        ║")
    print("╠══════════════════════════════════════════════════════════════════╣")
    print(f"║  URL   : {CONFIG['SONARQUBE_URL']}")
    print(f"║  Host  : {host}:{port}")
    print(f"║  Tools : {len(ALL_TOOLS)}")
    print(f"║  Auth  : {'Bearer token enabled' if CONFIG['MCP_SECRET_TOKEN'] else 'DISABLED'}")
    print("╠══════════════════════════════════════════════════════════════════╣")
    print(f"║  SSE  endpoint : http://{host}:{port}/sse")
    print(f"║  MCP  endpoint : http://{host}:{port}/mcp")
    print(f"║  Health        : http://{host}:{port}/health")
    print("╚══════════════════════════════════════════════════════════════════╝")

    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
