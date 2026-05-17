#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║        ServiceNow MCP Server  —  Full Production Build          ║
║                                                                  ║
║  • 58 tools covering every major ServiceNow operation            ║
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
    "SNOW_URL":         os.environ.get("SNOW_URL",         "https://dev375632.service-now.com"),
    "SNOW_USERNAME":    os.environ.get("SNOW_USERNAME",    "admin"),
    "SNOW_PASSWORD":    os.environ.get("SNOW_PASSWORD",    "kNy2EbSz+@S3"),
    "SNOW_VERIFY_SSL":  os.environ.get("SNOW_VERIFY_SSL",  "true"),
    "HOST":             os.environ.get("HOST",             "0.0.0.0"),
    "PORT":             os.environ.get("PORT",             "6503"),
    "MCP_SECRET_TOKEN": os.environ.get("MCP_SECRET_TOKEN", "1234456789"),
}

_missing = [k for k in ("SNOW_URL", "SNOW_USERNAME", "SNOW_PASSWORD") if not CONFIG[k]]
if _missing:
    import sys
    print(f"ERROR: Required environment variables not set: {', '.join(_missing)}")
    sys.exit(1)

# ══════════════════════════════════════════════════════════════════
#  SERVICENOW CLIENT
# ══════════════════════════════════════════════════════════════════

class SNowClient:
    def __init__(self, url: str, username: str, password: str, verify_ssl: bool = True):
        self.base_url   = url.rstrip("/")
        self.verify_ssl = verify_ssl
        creds           = f"{username}:{password}"
        self._auth      = base64.b64encode(creds.encode()).decode()

    def _ctx(self):
        if not self.verify_ssl:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode    = ssl.CERT_NONE
            return ctx
        return None

    def _req(self, path: str, method: str = "GET", data: Optional[bytes] = None,
             content_type: str = "application/json") -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = {
            "Authorization": f"Basic {self._auth}",
            "Content-Type":  content_type,
            "Accept":        "application/json",
            "User-Agent":    "snow-mcp-server/1.0",
        }
        req = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(req, context=self._ctx(), timeout=30) as r:
                body = r.read()
                return json.loads(body) if body else {}
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code} {e.reason}: {body[:500]}")

    def get(self, path):            return self._req(path)
    def post(self, path, payload):  return self._req(path, "POST", json.dumps(payload).encode())
    def patch(self, path, payload): return self._req(path, "PATCH", json.dumps(payload).encode())
    def put(self, path, payload):   return self._req(path, "PUT", json.dumps(payload).encode())
    def delete(self, path):         return self._req(path, "DELETE")

    def table_url(self, table: str, sys_id: str = "", extra_qs: str = "") -> str:
        base = f"api/now/table/{table}"
        if sys_id: base += f"/{sys_id}"
        if extra_qs: base += f"?{extra_qs}"
        return base

    def query_params(self, a: dict, field_map: dict = None) -> str:
        """Build sysparm_* query params from args dict."""
        parts = []
        fm = field_map or {}
        for k, v in a.items():
            snow_k = fm.get(k, k)
            if v is not None and k not in ("table", "sys_id"):
                parts.append(f"{snow_k}={v}")
        return "&".join(parts)


def _client() -> SNowClient:
    verify = CONFIG["SNOW_VERIFY_SSL"].lower() != "false"
    return SNowClient(CONFIG["SNOW_URL"], CONFIG["SNOW_USERNAME"], CONFIG["SNOW_PASSWORD"], verify)


# ══════════════════════════════════════════════════════════════════
#  RESULT HELPERS
# ══════════════════════════════════════════════════════════════════

@dataclass
class TC:
    type: str = "text"
    text: str = ""

@dataclass
class CTR:
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

def _result(data):
    """Unwrap ServiceNow's {'result': ...} wrapper."""
    if isinstance(data, dict) and "result" in data:
        return data["result"]
    return data


# ══════════════════════════════════════════════════════════════════
#  TOOL REGISTRY  (58 tools)
# ══════════════════════════════════════════════════════════════════

def _t(name, desc, props=None, required=None):
    schema = {"type": "object", "properties": props or {}}
    if required:
        schema["required"] = required
    return Tool(name=name, description=desc, inputSchema=schema)

def _p(type_, desc):
    return {"type": type_, "description": desc}

ALL_TOOLS = [
    # ── TABLE API (generic - works on ANY table) ───────────────────
    _t("table_list",
       "Query any ServiceNow table using the Table API. Supports filtering, sorting, field selection.",
       {"table":             _p("string",  "Table name (e.g. incident, change_request, sys_user)"),
        "sysparm_query":     _p("string",  "Encoded query string (e.g. active=true^priority=1)"),
        "sysparm_fields":    _p("string",  "Comma-separated fields to return"),
        "sysparm_limit":     _p("integer", "Max records to return (default 10, max 1000)"),
        "sysparm_offset":    _p("integer", "Pagination offset"),
        "sysparm_display_value": _p("string", "Return display values: true, false, or all"),
        "sysparm_order_by":  _p("string",  "Field to order by"),
        "sysparm_order_direction": _p("string", "asc or desc")},
       ["table"]),

    _t("table_get",
       "Get a single record from any ServiceNow table by sys_id.",
       {"table":             _p("string", "Table name"),
        "sys_id":            _p("string", "Record sys_id"),
        "sysparm_fields":    _p("string", "Comma-separated fields to return"),
        "sysparm_display_value": _p("string", "true, false, or all")},
       ["table", "sys_id"]),

    _t("table_create",
       "Create a new record in any ServiceNow table.",
       {"table":   _p("string", "Table name"),
        "payload": _p("object", "Field name/value pairs for the new record")},
       ["table", "payload"]),

    _t("table_update",
       "Update an existing record in any ServiceNow table.",
       {"table":   _p("string", "Table name"),
        "sys_id":  _p("string", "Record sys_id"),
        "payload": _p("object", "Fields to update")},
       ["table", "sys_id", "payload"]),

    _t("table_delete",
       "Delete a record from any ServiceNow table by sys_id.",
       {"table":  _p("string", "Table name"),
        "sys_id": _p("string", "Record sys_id")},
       ["table", "sys_id"]),

    # ── INCIDENTS ─────────────────────────────────────────────────
    _t("list_incidents",
       "List incidents with optional filters.",
       {"state":        _p("string",  "State filter: 1=New 2=In Progress 3=On Hold 6=Resolved 7=Closed"),
        "priority":     _p("string",  "Priority: 1=Critical 2=High 3=Medium 4=Low"),
        "assigned_to":  _p("string",  "Assigned user (username or sys_id)"),
        "caller_id":    _p("string",  "Caller username or sys_id"),
        "category":     _p("string",  "Category"),
        "limit":        _p("integer", "Max results (default 20)"),
        "query":        _p("string",  "Additional encoded query")}),

    _t("get_incident",
       "Get a specific incident by number (e.g. INC0012345) or sys_id.",
       {"number_or_id": _p("string", "Incident number (INC...) or sys_id")},
       ["number_or_id"]),

    _t("create_incident",
       "Create a new incident.",
       {"short_description": _p("string",  "Brief summary (required)"),
        "description":       _p("string",  "Full description"),
        "caller_id":         _p("string",  "Caller username or sys_id"),
        "category":          _p("string",  "Category"),
        "subcategory":       _p("string",  "Subcategory"),
        "priority":          _p("string",  "1=Critical 2=High 3=Medium 4=Low"),
        "urgency":           _p("string",  "1=High 2=Medium 3=Low"),
        "impact":            _p("string",  "1=High 2=Medium 3=Low"),
        "assignment_group":  _p("string",  "Assignment group name or sys_id"),
        "assigned_to":       _p("string",  "Assigned user username or sys_id"),
        "cmdb_ci":           _p("string",  "Configuration item name or sys_id")},
       ["short_description"]),

    _t("update_incident",
       "Update an existing incident.",
       {"number_or_id":      _p("string", "Incident number or sys_id"),
        "short_description": _p("string", "New short description"),
        "description":       _p("string", "Updated description"),
        "state":             _p("string", "New state value"),
        "priority":          _p("string", "New priority"),
        "close_code":        _p("string", "Resolution code (for resolving)"),
        "close_notes":       _p("string", "Resolution notes"),
        "assigned_to":       _p("string", "Reassign to username or sys_id"),
        "assignment_group":  _p("string", "New assignment group")},
       ["number_or_id"]),

    _t("resolve_incident",
       "Resolve an incident with resolution code and notes.",
       {"number_or_id": _p("string", "Incident number or sys_id"),
        "close_code":   _p("string", "Resolution code (e.g. 'Solved (Permanently)')"),
        "close_notes":  _p("string", "Resolution notes/explanation")},
       ["number_or_id", "close_code", "close_notes"]),

    _t("add_incident_work_note",
       "Add a work note (internal) to an incident.",
       {"number_or_id": _p("string", "Incident number or sys_id"),
        "work_notes":   _p("string", "Work note content")},
       ["number_or_id", "work_notes"]),

    _t("add_incident_comment",
       "Add a customer-visible comment to an incident.",
       {"number_or_id": _p("string", "Incident number or sys_id"),
        "comments":     _p("string", "Comment content")},
       ["number_or_id", "comments"]),

    # ── CHANGE REQUESTS ───────────────────────────────────────────
    _t("list_changes",
       "List change requests.",
       {"state":    _p("string",  "State: -5=New -4=Assess -3=Authorize -2=Scheduled -1=Implement 0=Review 3=Closed"),
        "type":     _p("string",  "Type: normal, standard, emergency"),
        "priority": _p("string",  "1=Critical 2=High 3=Medium 4=Low"),
        "limit":    _p("integer", "Max results"),
        "query":    _p("string",  "Additional query")}),

    _t("get_change",
       "Get a change request by number (CHG...) or sys_id.",
       {"number_or_id": _p("string", "Change number or sys_id")},
       ["number_or_id"]),

    _t("create_change",
       "Create a new change request.",
       {"short_description":    _p("string",  "Summary"),
        "description":          _p("string",  "Full description"),
        "type":                 _p("string",  "normal, standard, or emergency"),
        "category":             _p("string",  "Category"),
        "priority":             _p("string",  "Priority level"),
        "risk":                 _p("string",  "1=High 2=Medium 3=Low 4=Very Low"),
        "impact":               _p("string",  "1=High 2=Medium 3=Low"),
        "assignment_group":     _p("string",  "Assignment group"),
        "assigned_to":          _p("string",  "Assigned user"),
        "start_date":           _p("string",  "Planned start datetime (YYYY-MM-DD HH:MM:SS)"),
        "end_date":             _p("string",  "Planned end datetime"),
        "implementation_plan":  _p("string",  "Steps to implement"),
        "backout_plan":         _p("string",  "Rollback plan"),
        "test_plan":            _p("string",  "Testing plan")},
       ["short_description"]),

    _t("update_change",
       "Update an existing change request.",
       {"number_or_id":      _p("string", "Change number or sys_id"),
        "short_description": _p("string", "New summary"),
        "state":             _p("string", "New state value"),
        "close_code":        _p("string", "Closure code"),
        "close_notes":       _p("string", "Closure notes"),
        "work_notes":        _p("string", "Work notes to add")},
       ["number_or_id"]),

    _t("add_change_work_note",
       "Add a work note to a change request.",
       {"number_or_id": _p("string", "Change number or sys_id"),
        "work_notes":   _p("string", "Work note text")},
       ["number_or_id", "work_notes"]),

    # ── PROBLEMS ──────────────────────────────────────────────────
    _t("list_problems",
       "List problem records.",
       {"state":    _p("string",  "State: 101=Open 102=Known Error 103=Pending Change 104=Closed/Resolved"),
        "priority": _p("string",  "Priority level"),
        "limit":    _p("integer", "Max results"),
        "query":    _p("string",  "Additional query")}),

    _t("get_problem",
       "Get a problem record by number (PRB...) or sys_id.",
       {"number_or_id": _p("string", "Problem number or sys_id")},
       ["number_or_id"]),

    _t("create_problem",
       "Create a new problem record.",
       {"short_description": _p("string", "Summary"),
        "description":       _p("string", "Detailed description"),
        "category":          _p("string", "Category"),
        "priority":          _p("string", "Priority"),
        "assignment_group":  _p("string", "Assignment group"),
        "assigned_to":       _p("string", "Assigned user")},
       ["short_description"]),

    _t("update_problem",
       "Update a problem record.",
       {"number_or_id":      _p("string", "Problem number or sys_id"),
        "short_description": _p("string", "New summary"),
        "state":             _p("string", "New state"),
        "work_notes":        _p("string", "Work notes"),
        "cause_notes":       _p("string", "Root cause notes"),
        "fix_notes":         _p("string", "Fix notes")},
       ["number_or_id"]),

    # ── SERVICE REQUESTS ──────────────────────────────────────────
    _t("list_requests",
       "List service requests (sc_request).",
       {"state":    _p("string",  "State value"),
        "limit":    _p("integer", "Max results"),
        "query":    _p("string",  "Additional query")}),

    _t("get_request",
       "Get a service request by number (REQ...) or sys_id.",
       {"number_or_id": _p("string", "Request number or sys_id")},
       ["number_or_id"]),

    _t("list_request_items",
       "List requested items (sc_req_item) for a service request.",
       {"request_number_or_id": _p("string",  "Parent request number or sys_id"),
        "limit":                _p("integer", "Max results")}),

    _t("get_request_item",
       "Get a specific requested item (RITM...) by number or sys_id.",
       {"number_or_id": _p("string", "RITM number or sys_id")},
       ["number_or_id"]),

    _t("update_request_item",
       "Update a requested item status or fields.",
       {"number_or_id": _p("string", "RITM number or sys_id"),
        "state":        _p("string", "State value"),
        "work_notes":   _p("string", "Work notes"),
        "comments":     _p("string", "Comments")},
       ["number_or_id"]),

    # ── TASKS (generic) ───────────────────────────────────────────
    _t("list_tasks",
       "List task records (task table — parent of incident, change, problem, etc.).",
       {"assigned_to":    _p("string",  "Assigned user"),
        "assignment_group":_p("string", "Assignment group"),
        "state":          _p("string",  "State filter"),
        "limit":          _p("integer", "Max results"),
        "query":          _p("string",  "Encoded query")}),

    _t("get_task",
       "Get a task record by number or sys_id.",
       {"number_or_id": _p("string", "Task number or sys_id")},
       ["number_or_id"]),

    # ── CMDB ──────────────────────────────────────────────────────
    _t("list_ci",
       "List Configuration Items from the CMDB.",
       {"ci_class":  _p("string",  "CMDB class name (e.g. cmdb_ci_server, cmdb_ci_application)"),
        "name":      _p("string",  "CI name filter (partial match supported)"),
        "query":     _p("string",  "Additional encoded query"),
        "limit":     _p("integer", "Max results"),
        "fields":    _p("string",  "Comma-separated fields to return")},
       ["ci_class"]),

    _t("get_ci",
       "Get a specific Configuration Item by sys_id.",
       {"sys_id":   _p("string", "CI sys_id"),
        "ci_class": _p("string", "CMDB class (default: cmdb_ci)")},
       ["sys_id"]),

    _t("create_ci",
       "Create a new Configuration Item.",
       {"ci_class": _p("string", "CMDB class name"),
        "payload":  _p("object", "CI field name/value pairs")},
       ["ci_class", "payload"]),

    _t("update_ci",
       "Update an existing Configuration Item.",
       {"sys_id":   _p("string", "CI sys_id"),
        "ci_class": _p("string", "CMDB class name"),
        "payload":  _p("object", "Fields to update")},
       ["sys_id", "ci_class", "payload"]),

    _t("get_ci_relationships",
       "Get relationships for a CI (upstream/downstream dependencies).",
       {"sys_id": _p("string", "CI sys_id")}, ["sys_id"]),

    # ── USERS ─────────────────────────────────────────────────────
    _t("list_users",
       "List user accounts.",
       {"active":    _p("boolean", "Filter active users only"),
        "user_name": _p("string",  "Username search filter"),
        "email":     _p("string",  "Email search filter"),
        "limit":     _p("integer", "Max results"),
        "query":     _p("string",  "Additional encoded query")}),

    _t("get_user",
       "Get a user by username or sys_id.",
       {"username_or_id": _p("string", "Username or sys_id")},
       ["username_or_id"]),

    _t("create_user",
       "Create a new user account.",
       {"user_name":  _p("string",  "Username (login name)"),
        "first_name": _p("string",  "First name"),
        "last_name":  _p("string",  "Last name"),
        "email":      _p("string",  "Email address"),
        "title":      _p("string",  "Job title"),
        "department": _p("string",  "Department"),
        "active":     _p("boolean", "Active status (default true)")},
       ["user_name", "first_name", "last_name", "email"]),

    _t("update_user",
       "Update a user account.",
       {"username_or_id": _p("string",  "Username or sys_id"),
        "email":          _p("string",  "New email"),
        "title":          _p("string",  "New title"),
        "active":         _p("boolean", "Active/inactive"),
        "manager":        _p("string",  "Manager sys_id or username")},
       ["username_or_id"]),

    _t("get_user_roles",
       "Get roles assigned to a user.",
       {"username_or_id": _p("string", "Username or sys_id")},
       ["username_or_id"]),

    # ── GROUPS ────────────────────────────────────────────────────
    _t("list_groups",
       "List user groups / assignment groups.",
       {"name":    _p("string",  "Group name filter"),
        "active":  _p("boolean", "Active groups only"),
        "limit":   _p("integer", "Max results"),
        "query":   _p("string",  "Additional query")}),

    _t("get_group",
       "Get a group by name or sys_id.",
       {"name_or_id": _p("string", "Group name or sys_id")},
       ["name_or_id"]),

    _t("get_group_members",
       "Get members of a specific group.",
       {"group_name_or_id": _p("string", "Group name or sys_id")},
       ["group_name_or_id"]),

    # ── KNOWLEDGE BASE ────────────────────────────────────────────
    _t("list_kb_articles",
       "List knowledge base articles.",
       {"query":       _p("string",  "Search text"),
        "kb_category": _p("string",  "Knowledge base category sys_id"),
        "active":      _p("boolean", "Active articles only (default true)"),
        "limit":       _p("integer", "Max results")}),

    _t("get_kb_article",
       "Get a knowledge base article by number (KB...) or sys_id.",
       {"number_or_id": _p("string", "KB number or sys_id")},
       ["number_or_id"]),

    _t("create_kb_article",
       "Create a knowledge base article.",
       {"short_description": _p("string", "Article title"),
        "text":              _p("string", "Article body (HTML or plain text)"),
        "kb_category":       _p("string", "Category sys_id"),
        "workflow_state":    _p("string", "draft or published (default: draft)")},
       ["short_description", "text"]),

    # ── SLA / OLA ─────────────────────────────────────────────────
    _t("list_sla_definitions",
       "List SLA/OLA definitions.",
       {"type":  _p("string",  "SLA type: SLA or OLA"),
        "limit": _p("integer", "Max results")}),

    _t("get_task_sla",
       "Get SLA timers for a specific task/incident.",
       {"task_sys_id": _p("string", "Task or incident sys_id")},
       ["task_sys_id"]),

    # ── CATALOG ───────────────────────────────────────────────────
    _t("list_catalog_items",
       "List service catalog items.",
       {"category":     _p("string",  "Category sys_id"),
        "search_term":  _p("string",  "Search by name"),
        "limit":        _p("integer", "Max results")}),

    _t("get_catalog_item",
       "Get a service catalog item by sys_id.",
       {"sys_id": _p("string", "Catalog item sys_id")}, ["sys_id"]),

    _t("submit_catalog_request",
       "Submit a catalog item request.",
       {"catalog_item_sys_id": _p("string", "Catalog item sys_id"),
        "variables":           _p("object", "Variable name/value pairs for the request form"),
        "quantity":            _p("integer","Quantity (default 1)"),
        "requested_for":       _p("string", "User sys_id (defaults to self)")},
       ["catalog_item_sys_id"]),

    # ── SCRIPT EXECUTION ──────────────────────────────────────────
    _t("run_script",
       "Execute a server-side GlideScript via the Scripted REST API or Script Executor (requires admin).",
       {"script": _p("string", "GlideScript / JavaScript to execute on the server")},
       ["script"]),

    # ── ATTACHMENTS ───────────────────────────────────────────────
    _t("list_attachments",
       "List attachments for a record.",
       {"table_name":   _p("string", "Table name (e.g. incident)"),
        "table_sys_id": _p("string", "Record sys_id")},
       ["table_name", "table_sys_id"]),

    # ── NOTIFICATIONS ─────────────────────────────────────────────
    _t("list_notifications",
       "List notification definitions.",
       {"active": _p("boolean", "Active only"),
        "limit":  _p("integer", "Max results")}),

    # ── REPORTS ───────────────────────────────────────────────────
    _t("list_reports",
       "List saved reports.",
       {"user": _p("string",  "Filter by owner username"),
        "limit":_p("integer", "Max results")}),

    # ── SYSTEM PROPERTIES ─────────────────────────────────────────
    _t("get_sys_property",
       "Get a system property value by name.",
       {"name": _p("string", "Property name (e.g. glide.application.name)")},
       ["name"]),

    _t("set_sys_property",
       "Set a system property value (requires admin).",
       {"name":  _p("string", "Property name"),
        "value": _p("string", "New value")},
       ["name", "value"]),

    # ── HEALTH CHECK ──────────────────────────────────────────────
    _t("health_check",
       "Verify connectivity to ServiceNow and return instance metadata."),

    # ── AGGREGATE API ─────────────────────────────────────────────
    _t("aggregate",
       "Run aggregate queries (COUNT, AVG, SUM, MIN, MAX) on any table.",
       {"table":              _p("string",  "Table name"),
        "sysparm_query":      _p("string",  "Filter query"),
        "sysparm_count":      _p("boolean", "Include count"),
        "sysparm_avg_fields": _p("string",  "Comma-separated fields to average"),
        "sysparm_sum_fields": _p("string",  "Comma-separated fields to sum"),
        "sysparm_group_by":   _p("string",  "Field to group by")},
       ["table"]),

    # ── IMPORT SETS ───────────────────────────────────────────────
    _t("list_import_sets",
       "List recent import set runs.",
       {"limit": _p("integer", "Max results")}),

    # ── WORKFLOW / FLOW ───────────────────────────────────────────
    _t("list_workflow_contexts",
       "List active workflow contexts (running workflows).",
       {"table_name":   _p("string",  "Table the workflow is running on"),
        "record_sys_id":_p("string",  "Specific record sys_id"),
        "limit":        _p("integer", "Max results")}),
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


def _resolve_number_or_id(c: SNowClient, table: str, field: str, value: str):
    """Return sys_id — if value looks like a number (INC...) query by that field."""
    upper = value.upper()
    if any(upper.startswith(p) for p in ("INC", "CHG", "PRB", "REQ", "RITM", "KB", "TASK")):
        result = _result(c.get(
            c.table_url(table, extra_qs=f"sysparm_query={field}={value}&sysparm_fields=sys_id&sysparm_limit=1")
        ))
        if result:
            return result[0]["sys_id"] if isinstance(result, list) else result.get("sys_id", value)
    return value  # assume it's already a sys_id


async def _dispatch(name: str, a: dict, c: SNowClient) -> CTR:

    # ── GENERIC TABLE API ─────────────────────────────────────────
    if name == "table_list":
        table = a.pop("table")
        parts = []
        for k, v in a.items():
            if v is not None: parts.append(f"{k}={v}")
        qs = "&".join(parts)
        return ok(_result(c.get(c.table_url(table, extra_qs=qs))))

    if name == "table_get":
        table  = a["table"]
        sys_id = a["sys_id"]
        parts  = []
        if a.get("sysparm_fields"):       parts.append(f"sysparm_fields={a['sysparm_fields']}")
        if a.get("sysparm_display_value"):parts.append(f"sysparm_display_value={a['sysparm_display_value']}")
        qs = "&".join(parts)
        return ok(_result(c.get(c.table_url(table, sys_id, qs))))

    if name == "table_create":
        return ok(_result(c.post(c.table_url(a["table"]), a["payload"])))

    if name == "table_update":
        return ok(_result(c.patch(c.table_url(a["table"], a["sys_id"]), a["payload"])))

    if name == "table_delete":
        c.delete(c.table_url(a["table"], a["sys_id"]))
        return ok(f"Record {a['sys_id']} deleted from {a['table']}.")

    # ── INCIDENTS ─────────────────────────────────────────────────
    if name == "list_incidents":
        parts = []
        q_parts = []
        if a.get("state"):       q_parts.append(f"state={a['state']}")
        if a.get("priority"):    q_parts.append(f"priority={a['priority']}")
        if a.get("assigned_to"): q_parts.append(f"assigned_to.user_name={a['assigned_to']}")
        if a.get("caller_id"):   q_parts.append(f"caller_id.user_name={a['caller_id']}")
        if a.get("category"):    q_parts.append(f"category={a['category']}")
        if a.get("query"):       q_parts.append(a["query"])
        if q_parts: parts.append(f"sysparm_query={'%5E'.join(q_parts)}")
        parts.append(f"sysparm_limit={a.get('limit', 20)}")
        parts.append("sysparm_display_value=true")
        return ok(_result(c.get(c.table_url("incident", extra_qs="&".join(parts)))))

    if name == "get_incident":
        v = a["number_or_id"]
        if v.upper().startswith("INC"):
            data = _result(c.get(c.table_url("incident",
                           extra_qs=f"sysparm_query=number={v}&sysparm_display_value=true&sysparm_limit=1")))
            return ok(data[0] if isinstance(data, list) and data else data)
        return ok(_result(c.get(c.table_url("incident", v, "sysparm_display_value=true"))))

    if name == "create_incident":
        payload = {k: v for k, v in {
            "short_description": a.get("short_description"),
            "description":       a.get("description"),
            "caller_id":         a.get("caller_id"),
            "category":          a.get("category"),
            "subcategory":       a.get("subcategory"),
            "priority":          a.get("priority"),
            "urgency":           a.get("urgency"),
            "impact":            a.get("impact"),
            "assignment_group":  a.get("assignment_group"),
            "assigned_to":       a.get("assigned_to"),
            "cmdb_ci":           a.get("cmdb_ci"),
        }.items() if v is not None}
        return ok(_result(c.post(c.table_url("incident"), payload)))

    if name == "update_incident":
        v      = a["number_or_id"]
        sys_id = _resolve_number_or_id(c, "incident", "number", v)
        payload = {k: val for k, val in {
            "short_description": a.get("short_description"),
            "description":       a.get("description"),
            "state":             a.get("state"),
            "priority":          a.get("priority"),
            "close_code":        a.get("close_code"),
            "close_notes":       a.get("close_notes"),
            "assigned_to":       a.get("assigned_to"),
            "assignment_group":  a.get("assignment_group"),
        }.items() if val is not None}
        return ok(_result(c.patch(c.table_url("incident", sys_id), payload)))

    if name == "resolve_incident":
        v      = a["number_or_id"]
        sys_id = _resolve_number_or_id(c, "incident", "number", v)
        return ok(_result(c.patch(c.table_url("incident", sys_id), {
            "state": "6", "close_code": a["close_code"], "close_notes": a["close_notes"],
        })))

    if name == "add_incident_work_note":
        sys_id = _resolve_number_or_id(c, "incident", "number", a["number_or_id"])
        return ok(_result(c.patch(c.table_url("incident", sys_id), {"work_notes": a["work_notes"]})))

    if name == "add_incident_comment":
        sys_id = _resolve_number_or_id(c, "incident", "number", a["number_or_id"])
        return ok(_result(c.patch(c.table_url("incident", sys_id), {"comments": a["comments"]})))

    # ── CHANGE REQUESTS ───────────────────────────────────────────
    if name == "list_changes":
        q_parts = []
        if a.get("state"):    q_parts.append(f"state={a['state']}")
        if a.get("type"):     q_parts.append(f"type={a['type']}")
        if a.get("priority"): q_parts.append(f"priority={a['priority']}")
        if a.get("query"):    q_parts.append(a["query"])
        parts = [f"sysparm_limit={a.get('limit', 20)}", "sysparm_display_value=true"]
        if q_parts: parts.insert(0, f"sysparm_query={'%5E'.join(q_parts)}")
        return ok(_result(c.get(c.table_url("change_request", extra_qs="&".join(parts)))))

    if name == "get_change":
        v = a["number_or_id"]
        if v.upper().startswith("CHG"):
            data = _result(c.get(c.table_url("change_request",
                           extra_qs=f"sysparm_query=number={v}&sysparm_display_value=true&sysparm_limit=1")))
            return ok(data[0] if isinstance(data, list) and data else data)
        return ok(_result(c.get(c.table_url("change_request", v, "sysparm_display_value=true"))))

    if name == "create_change":
        payload = {k: val for k, val in {
            "short_description": a.get("short_description"),
            "description":       a.get("description"),
            "type":              a.get("type"),
            "category":          a.get("category"),
            "priority":          a.get("priority"),
            "risk":              a.get("risk"),
            "impact":            a.get("impact"),
            "assignment_group":  a.get("assignment_group"),
            "assigned_to":       a.get("assigned_to"),
            "start_date":        a.get("start_date"),
            "end_date":          a.get("end_date"),
            "implementation_plan": a.get("implementation_plan"),
            "backout_plan":      a.get("backout_plan"),
            "test_plan":         a.get("test_plan"),
        }.items() if val is not None}
        return ok(_result(c.post(c.table_url("change_request"), payload)))

    if name == "update_change":
        sys_id  = _resolve_number_or_id(c, "change_request", "number", a["number_or_id"])
        payload = {k: val for k, val in {
            "short_description": a.get("short_description"),
            "state":             a.get("state"),
            "close_code":        a.get("close_code"),
            "close_notes":       a.get("close_notes"),
            "work_notes":        a.get("work_notes"),
        }.items() if val is not None}
        return ok(_result(c.patch(c.table_url("change_request", sys_id), payload)))

    if name == "add_change_work_note":
        sys_id = _resolve_number_or_id(c, "change_request", "number", a["number_or_id"])
        return ok(_result(c.patch(c.table_url("change_request", sys_id), {"work_notes": a["work_notes"]})))

    # ── PROBLEMS ──────────────────────────────────────────────────
    if name == "list_problems":
        q_parts = []
        if a.get("state"):    q_parts.append(f"state={a['state']}")
        if a.get("priority"): q_parts.append(f"priority={a['priority']}")
        if a.get("query"):    q_parts.append(a["query"])
        parts = [f"sysparm_limit={a.get('limit', 20)}", "sysparm_display_value=true"]
        if q_parts: parts.insert(0, f"sysparm_query={'%5E'.join(q_parts)}")
        return ok(_result(c.get(c.table_url("problem", extra_qs="&".join(parts)))))

    if name == "get_problem":
        v = a["number_or_id"]
        if v.upper().startswith("PRB"):
            data = _result(c.get(c.table_url("problem",
                           extra_qs=f"sysparm_query=number={v}&sysparm_display_value=true&sysparm_limit=1")))
            return ok(data[0] if isinstance(data, list) and data else data)
        return ok(_result(c.get(c.table_url("problem", v, "sysparm_display_value=true"))))

    if name == "create_problem":
        payload = {k: val for k, val in {
            "short_description": a.get("short_description"),
            "description":       a.get("description"),
            "category":          a.get("category"),
            "priority":          a.get("priority"),
            "assignment_group":  a.get("assignment_group"),
            "assigned_to":       a.get("assigned_to"),
        }.items() if val is not None}
        return ok(_result(c.post(c.table_url("problem"), payload)))

    if name == "update_problem":
        sys_id  = _resolve_number_or_id(c, "problem", "number", a["number_or_id"])
        payload = {k: val for k, val in {
            "short_description": a.get("short_description"),
            "state":             a.get("state"),
            "work_notes":        a.get("work_notes"),
            "cause_notes":       a.get("cause_notes"),
            "fix_notes":         a.get("fix_notes"),
        }.items() if val is not None}
        return ok(_result(c.patch(c.table_url("problem", sys_id), payload)))

    # ── SERVICE REQUESTS ──────────────────────────────────────────
    if name == "list_requests":
        q_parts = []
        if a.get("state"): q_parts.append(f"state={a['state']}")
        if a.get("query"): q_parts.append(a["query"])
        parts = [f"sysparm_limit={a.get('limit', 20)}", "sysparm_display_value=true"]
        if q_parts: parts.insert(0, f"sysparm_query={'%5E'.join(q_parts)}")
        return ok(_result(c.get(c.table_url("sc_request", extra_qs="&".join(parts)))))

    if name == "get_request":
        v = a["number_or_id"]
        if v.upper().startswith("REQ"):
            data = _result(c.get(c.table_url("sc_request",
                           extra_qs=f"sysparm_query=number={v}&sysparm_display_value=true&sysparm_limit=1")))
            return ok(data[0] if isinstance(data, list) and data else data)
        return ok(_result(c.get(c.table_url("sc_request", v, "sysparm_display_value=true"))))

    if name == "list_request_items":
        parent_id = _resolve_number_or_id(c, "sc_request", "number", a["request_number_or_id"])
        lim = a.get("limit", 20)
        return ok(_result(c.get(c.table_url("sc_req_item",
                          extra_qs=f"sysparm_query=request={parent_id}&sysparm_limit={lim}&sysparm_display_value=true"))))

    if name == "get_request_item":
        v = a["number_or_id"]
        if v.upper().startswith("RITM"):
            data = _result(c.get(c.table_url("sc_req_item",
                           extra_qs=f"sysparm_query=number={v}&sysparm_display_value=true&sysparm_limit=1")))
            return ok(data[0] if isinstance(data, list) and data else data)
        return ok(_result(c.get(c.table_url("sc_req_item", v, "sysparm_display_value=true"))))

    if name == "update_request_item":
        sys_id  = _resolve_number_or_id(c, "sc_req_item", "number", a["number_or_id"])
        payload = {k: val for k, val in {
            "state": a.get("state"), "work_notes": a.get("work_notes"),
            "comments": a.get("comments"),
        }.items() if val is not None}
        return ok(_result(c.patch(c.table_url("sc_req_item", sys_id), payload)))

    # ── TASKS ─────────────────────────────────────────────────────
    if name == "list_tasks":
        q_parts = []
        if a.get("assigned_to"):     q_parts.append(f"assigned_to.user_name={a['assigned_to']}")
        if a.get("assignment_group"):q_parts.append(f"assignment_group.name={a['assignment_group']}")
        if a.get("state"):           q_parts.append(f"state={a['state']}")
        if a.get("query"):           q_parts.append(a["query"])
        parts = [f"sysparm_limit={a.get('limit', 20)}", "sysparm_display_value=true"]
        if q_parts: parts.insert(0, f"sysparm_query={'%5E'.join(q_parts)}")
        return ok(_result(c.get(c.table_url("task", extra_qs="&".join(parts)))))

    if name == "get_task":
        v = a["number_or_id"]
        data = _result(c.get(c.table_url("task",
                       extra_qs=f"sysparm_query=number={v}&sysparm_display_value=true&sysparm_limit=1")))
        if isinstance(data, list) and data: return ok(data[0])
        return ok(_result(c.get(c.table_url("task", v, "sysparm_display_value=true"))))

    # ── CMDB ──────────────────────────────────────────────────────
    if name == "list_ci":
        ci_class = a["ci_class"]
        q_parts  = []
        if a.get("name"):  q_parts.append(f"nameLIKE{a['name']}")
        if a.get("query"): q_parts.append(a["query"])
        parts = [f"sysparm_limit={a.get('limit', 20)}"]
        if a.get("fields"): parts.append(f"sysparm_fields={a['fields']}")
        if q_parts: parts.insert(0, f"sysparm_query={'%5E'.join(q_parts)}")
        return ok(_result(c.get(c.table_url(ci_class, extra_qs="&".join(parts)))))

    if name == "get_ci":
        ci_class = a.get("ci_class", "cmdb_ci")
        return ok(_result(c.get(c.table_url(ci_class, a["sys_id"]))))

    if name == "create_ci":
        return ok(_result(c.post(c.table_url(a["ci_class"]), a["payload"])))

    if name == "update_ci":
        return ok(_result(c.patch(c.table_url(a["ci_class"], a["sys_id"]), a["payload"])))

    if name == "get_ci_relationships":
        return ok(_result(c.get(c.table_url("cmdb_rel_ci",
                          extra_qs=f"sysparm_query=parent={a['sys_id']}ORchild={a['sys_id']}&sysparm_limit=50"))))

    # ── USERS ─────────────────────────────────────────────────────
    if name == "list_users":
        q_parts = []
        if a.get("active") is not None: q_parts.append(f"active={str(a['active']).lower()}")
        if a.get("user_name"):          q_parts.append(f"user_nameLIKE{a['user_name']}")
        if a.get("email"):              q_parts.append(f"emailLIKE{a['email']}")
        if a.get("query"):              q_parts.append(a["query"])
        parts = [f"sysparm_limit={a.get('limit', 20)}"]
        if q_parts: parts.insert(0, f"sysparm_query={'%5E'.join(q_parts)}")
        return ok(_result(c.get(c.table_url("sys_user", extra_qs="&".join(parts)))))

    if name == "get_user":
        v = a["username_or_id"]
        # Try by username first
        data = _result(c.get(c.table_url("sys_user",
                       extra_qs=f"sysparm_query=user_name={v}&sysparm_limit=1")))
        if isinstance(data, list) and data: return ok(data[0])
        return ok(_result(c.get(c.table_url("sys_user", v))))

    if name == "create_user":
        payload = {k: val for k, val in {
            "user_name": a.get("user_name"), "first_name": a.get("first_name"),
            "last_name":  a.get("last_name"), "email": a.get("email"),
            "title":      a.get("title"),     "department": a.get("department"),
            "active":     str(a.get("active", True)).lower(),
        }.items() if val is not None}
        return ok(_result(c.post(c.table_url("sys_user"), payload)))

    if name == "update_user":
        v      = a["username_or_id"]
        data   = _result(c.get(c.table_url("sys_user",
                         extra_qs=f"sysparm_query=user_name={v}&sysparm_fields=sys_id&sysparm_limit=1")))
        sys_id = data[0]["sys_id"] if isinstance(data, list) and data else v
        payload = {k: val for k, val in {
            "email": a.get("email"), "title": a.get("title"),
            "active": str(a["active"]).lower() if a.get("active") is not None else None,
            "manager": a.get("manager"),
        }.items() if val is not None}
        return ok(_result(c.patch(c.table_url("sys_user", sys_id), payload)))

    if name == "get_user_roles":
        v    = a["username_or_id"]
        data = _result(c.get(c.table_url("sys_user",
                       extra_qs=f"sysparm_query=user_name={v}&sysparm_fields=sys_id&sysparm_limit=1")))
        sys_id = data[0]["sys_id"] if isinstance(data, list) and data else v
        return ok(_result(c.get(c.table_url("sys_user_has_role",
                          extra_qs=f"sysparm_query=user={sys_id}&sysparm_display_value=true"))))

    # ── GROUPS ────────────────────────────────────────────────────
    if name == "list_groups":
        q_parts = []
        if a.get("name"):              q_parts.append(f"nameLIKE{a['name']}")
        if a.get("active") is not None:q_parts.append(f"active={str(a['active']).lower()}")
        if a.get("query"):             q_parts.append(a["query"])
        parts = [f"sysparm_limit={a.get('limit', 20)}"]
        if q_parts: parts.insert(0, f"sysparm_query={'%5E'.join(q_parts)}")
        return ok(_result(c.get(c.table_url("sys_user_group", extra_qs="&".join(parts)))))

    if name == "get_group":
        v    = a["name_or_id"]
        data = _result(c.get(c.table_url("sys_user_group",
                       extra_qs=f"sysparm_query=name={v}&sysparm_limit=1")))
        if isinstance(data, list) and data: return ok(data[0])
        return ok(_result(c.get(c.table_url("sys_user_group", v))))

    if name == "get_group_members":
        v      = a["group_name_or_id"]
        data   = _result(c.get(c.table_url("sys_user_group",
                         extra_qs=f"sysparm_query=name={v}&sysparm_fields=sys_id&sysparm_limit=1")))
        sys_id = data[0]["sys_id"] if isinstance(data, list) and data else v
        return ok(_result(c.get(c.table_url("sys_user_grmember",
                          extra_qs=f"sysparm_query=group={sys_id}&sysparm_display_value=true"))))

    # ── KNOWLEDGE BASE ────────────────────────────────────────────
    if name == "list_kb_articles":
        q_parts = [f"active={str(a.get('active', True)).lower()}"]
        if a.get("kb_category"): q_parts.append(f"kb_category={a['kb_category']}")
        if a.get("query"):       q_parts.append(f"short_descriptionLIKE{a['query']}ORtextLIKE{a['query']}")
        parts = [f"sysparm_query={'%5E'.join(q_parts)}", f"sysparm_limit={a.get('limit', 20)}"]
        return ok(_result(c.get(c.table_url("kb_knowledge", extra_qs="&".join(parts)))))

    if name == "get_kb_article":
        v = a["number_or_id"]
        if v.upper().startswith("KB"):
            data = _result(c.get(c.table_url("kb_knowledge",
                           extra_qs=f"sysparm_query=number={v}&sysparm_limit=1")))
            return ok(data[0] if isinstance(data, list) and data else data)
        return ok(_result(c.get(c.table_url("kb_knowledge", v))))

    if name == "create_kb_article":
        payload = {
            "short_description": a["short_description"],
            "text":              a["text"],
            "workflow_state":    a.get("workflow_state", "draft"),
        }
        if a.get("kb_category"): payload["kb_category"] = a["kb_category"]
        return ok(_result(c.post(c.table_url("kb_knowledge"), payload)))

    # ── SLA ───────────────────────────────────────────────────────
    if name == "list_sla_definitions":
        q_parts = []
        if a.get("type"): q_parts.append(f"type={a['type']}")
        parts = [f"sysparm_limit={a.get('limit', 20)}"]
        if q_parts: parts.insert(0, f"sysparm_query={'%5E'.join(q_parts)}")
        return ok(_result(c.get(c.table_url("contract_sla", extra_qs="&".join(parts)))))

    if name == "get_task_sla":
        return ok(_result(c.get(c.table_url("task_sla",
                          extra_qs=f"sysparm_query=task={a['task_sys_id']}&sysparm_display_value=true"))))

    # ── CATALOG ───────────────────────────────────────────────────
    if name == "list_catalog_items":
        q_parts = ["active=true"]
        if a.get("category"):    q_parts.append(f"category={a['category']}")
        if a.get("search_term"): q_parts.append(f"nameLIKE{a['search_term']}")
        parts = [f"sysparm_query={'%5E'.join(q_parts)}", f"sysparm_limit={a.get('limit', 20)}"]
        return ok(_result(c.get(c.table_url("sc_cat_item", extra_qs="&".join(parts)))))

    if name == "get_catalog_item":
        return ok(_result(c.get(c.table_url("sc_cat_item", a["sys_id"]))))

    if name == "submit_catalog_request":
        payload = {
            "sysparm_quantity":      a.get("quantity", 1),
            "variables":             a.get("variables", {}),
        }
        if a.get("requested_for"): payload["sysparm_requested_for"] = a["requested_for"]
        return ok(_result(c.post(
            f"api/sn_sc/servicecatalog/items/{a['catalog_item_sys_id']}/order_now", payload
        )))

    # ── SCRIPT ────────────────────────────────────────────────────
    if name == "run_script":
        return ok(_result(c.post("api/now/v1/script", {"script": a["script"]})))

    # ── ATTACHMENTS ───────────────────────────────────────────────
    if name == "list_attachments":
        return ok(_result(c.get(
            f"api/now/attachment?sysparm_query=table_name={a['table_name']}%5Etable_sys_id={a['table_sys_id']}"
        )))

    # ── NOTIFICATIONS ─────────────────────────────────────────────
    if name == "list_notifications":
        parts = [f"sysparm_limit={a.get('limit', 20)}"]
        if a.get("active") is not None:
            parts.insert(0, f"sysparm_query=active={str(a['active']).lower()}")
        return ok(_result(c.get(c.table_url("sysevent_email_action", extra_qs="&".join(parts)))))

    # ── REPORTS ───────────────────────────────────────────────────
    if name == "list_reports":
        parts = [f"sysparm_limit={a.get('limit', 20)}"]
        if a.get("user"): parts.insert(0, f"sysparm_query=sys_created_by={a['user']}")
        return ok(_result(c.get(c.table_url("sys_report", extra_qs="&".join(parts)))))

    # ── SYSTEM PROPERTIES ─────────────────────────────────────────
    if name == "get_sys_property":
        data = _result(c.get(c.table_url("sys_properties",
                       extra_qs=f"sysparm_query=name={a['name']}&sysparm_fields=name,value&sysparm_limit=1")))
        return ok(data[0] if isinstance(data, list) and data else data)

    if name == "set_sys_property":
        data   = _result(c.get(c.table_url("sys_properties",
                         extra_qs=f"sysparm_query=name={a['name']}&sysparm_fields=sys_id&sysparm_limit=1")))
        sys_id = data[0]["sys_id"] if isinstance(data, list) and data else None
        if sys_id:
            return ok(_result(c.patch(c.table_url("sys_properties", sys_id), {"value": a["value"]})))
        return ok(_result(c.post(c.table_url("sys_properties"), {"name": a["name"], "value": a["value"]})))

    # ── HEALTH CHECK ──────────────────────────────────────────────
    if name == "health_check":
        data = _result(c.get("api/now/table/sys_properties?sysparm_query=name=glide.application.name"
                             "&sysparm_fields=value&sysparm_limit=1"))
        instance_name = data[0]["value"] if isinstance(data, list) and data else "unknown"
        return ok({"status": "ok", "instance": CONFIG["SNOW_URL"], "application": instance_name})

    # ── AGGREGATE ─────────────────────────────────────────────────
    if name == "aggregate":
        parts = []
        for k in ("sysparm_query", "sysparm_count", "sysparm_avg_fields",
                  "sysparm_sum_fields", "sysparm_group_by"):
            if a.get(k) is not None: parts.append(f"{k}={a[k]}")
        qs = "&".join(parts)
        return ok(_result(c.get(f"api/now/stats/{a['table']}{'?' + qs if qs else ''}")))

    # ── IMPORT SETS ───────────────────────────────────────────────
    if name == "list_import_sets":
        lim = a.get("limit", 20)
        return ok(_result(c.get(c.table_url("sys_import_set",
                          extra_qs=f"sysparm_limit={lim}&sysparm_order_by_direction=desc"))))

    # ── WORKFLOW CONTEXTS ─────────────────────────────────────────
    if name == "list_workflow_contexts":
        q_parts = []
        if a.get("table_name"):    q_parts.append(f"table_name={a['table_name']}")
        if a.get("record_sys_id"): q_parts.append(f"id={a['record_sys_id']}")
        parts = [f"sysparm_limit={a.get('limit', 20)}"]
        if q_parts: parts.insert(0, f"sysparm_query={'%5E'.join(q_parts)}")
        return ok(_result(c.get(c.table_url("wf_context", extra_qs="&".join(parts)))))

    return err(f"Unknown tool: {name}")


# ══════════════════════════════════════════════════════════════════
#  JSON-RPC 2.0 / SSE / STREAMABLE HTTP  (identical boilerplate)
# ══════════════════════════════════════════════════════════════════

def _rpc_ok(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}

def _rpc_err(req_id, code, msg):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": msg}}

def _tool_dict(t: Tool) -> dict:
    return {"name": t.name, "description": t.description, "inputSchema": t.inputSchema}

def _ctr_dict(r: CTR) -> dict:
    return {"content": [{"type": c.type, "text": c.text} for c in r.content], "isError": r.isError}

_sessions:     dict[str, asyncio.Queue] = {}
_mcp_sessions: dict[str, asyncio.Queue] = {}

@web.middleware
async def auth_middleware(request: web.Request, handler):
    if request.path in ("/health", "/"):
        return await handler(request)
    token = CONFIG.get("MCP_SECRET_TOKEN", "").strip()
    if token:
        if request.headers.get("Authorization", "") != f"Bearer {token}":
            return web.json_response({"error": "Unauthorized"}, status=401)
    return await handler(request)

async def handle_health(request):
    return web.json_response({
        "status": "ok", "server": "snow-mcp",
        "tools": len(ALL_TOOLS), "url": CONFIG["SNOW_URL"],
    })

async def handle_sse(request):
    sid = str(uuid.uuid4()); q: asyncio.Queue = asyncio.Queue()
    _sessions[sid] = q
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
        _sessions.pop(sid, None)
    return resp

async def handle_message(request):
    sid = request.rel_url.query.get("session_id"); q = _sessions.get(sid)
    try: body = await request.json()
    except Exception: return web.json_response({"error": "invalid JSON"}, status=400)
    req_id = body.get("id"); method = body.get("method", ""); params = body.get("params", {})
    resp = None
    if method == "initialize":
        resp = _rpc_ok(req_id, {"protocolVersion": "2025-03-26",
                                "capabilities": {"tools": {}},
                                "serverInfo": {"name": "snow-mcp", "version": "1.0.0"}})
    elif method == "notifications/initialized": return web.Response(status=202)
    elif method == "tools/list": resp = _rpc_ok(req_id, {"tools": [_tool_dict(t) for t in ALL_TOOLS]})
    elif method == "tools/call":
        result = await call_tool(params.get("name", ""), params.get("arguments", {}))
        resp   = _rpc_ok(req_id, _ctr_dict(result))
    elif method == "ping": resp = _rpc_ok(req_id, {})
    else: resp = _rpc_err(req_id, -32601, f"Method not found: {method}")
    if resp is not None:
        if q: await q.put(resp)
        else: return web.json_response(resp)
    return web.Response(status=202)

async def _process_jsonrpc(body):
    req_id = body.get("id"); method = body.get("method", ""); params = body.get("params", {})
    if method == "initialize":
        return _rpc_ok(req_id, {"protocolVersion": "2025-03-26",
                                "capabilities": {"tools": {}},
                                "serverInfo": {"name": "snow-mcp", "version": "1.0.0"}})
    if method in ("notifications/initialized", "notifications/cancelled"): return None
    if method == "tools/list": return _rpc_ok(req_id, {"tools": [_tool_dict(t) for t in ALL_TOOLS]})
    if method == "tools/call":
        result = await call_tool(params.get("name", ""), params.get("arguments", {}))
        return _rpc_ok(req_id, _ctr_dict(result))
    if method == "ping": return _rpc_ok(req_id, {})
    return _rpc_err(req_id, -32601, f"Method not found: {method}")

async def handle_mcp_post(request):
    try: body = await request.json()
    except Exception: return web.json_response(_rpc_err(None, -32700, "Parse error"), status=400)
    accept = request.headers.get("Accept", "application/json")
    is_batch = isinstance(body, list); items = body if is_batch else [body]
    responses = [r for r in [await _process_jsonrpc(i) for i in items] if r is not None]
    result_body = responses if is_batch else (responses[0] if responses else None)
    if "text/event-stream" in accept:
        sr = web.StreamResponse()
        sr.headers.update({"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                           "Access-Control-Allow-Origin": "*"})
        await sr.prepare(request)
        if result_body: await sr.write(f"data: {json.dumps(result_body)}\n\n".encode())
        return sr
    if result_body is None: return web.Response(status=202)
    return web.json_response(result_body)

async def handle_mcp_get(request):
    sid = str(uuid.uuid4()); q: asyncio.Queue = asyncio.Queue()
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

async def handle_mcp_delete(request):
    sid = request.rel_url.query.get("session_id", "")
    if sid in _mcp_sessions:
        _mcp_sessions.pop(sid, None); return web.Response(status=200, text="Session closed")
    return web.Response(status=404, text="Session not found")

async def handle_options(request):
    return web.Response(headers={"Access-Control-Allow-Origin": "*",
                                 "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                                 "Access-Control-Allow-Headers": "Content-Type, Authorization, Accept"})

async def main():
    port = int(CONFIG["PORT"]); host = CONFIG["HOST"]
    app  = web.Application(middlewares=[auth_middleware])
    app.router.add_get   ("/sse",     handle_sse)
    app.router.add_post  ("/message", handle_message)
    app.router.add_post  ("/mcp",     handle_mcp_post)
    app.router.add_get   ("/mcp",     handle_mcp_get)
    app.router.add_route ("DELETE", "/mcp", handle_mcp_delete)
    app.router.add_get   ("/health",  handle_health)
    app.router.add_get   ("/",        handle_health)
    app.router.add_route ("OPTIONS", "/{path_info:.*}", handle_options)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    print(f"╔══════════════════════════════════════════════╗")
    print(f"║     ServiceNow MCP Server — Running          ║")
    print(f"╠══════════════════════════════════════════════╣")
    print(f"║  POST  http://{host}:{port}/mcp")
    print(f"║  GET   http://{host}:{port}/mcp  (SSE)")
    print(f"║  GET   http://{host}:{port}/sse  (legacy)")
    print(f"║  Tools : {len(ALL_TOOLS)}")
    print(f"║  URL   : {CONFIG['SNOW_URL']}")
    print(f"╚══════════════════════════════════════════════╝")
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
