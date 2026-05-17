
#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║           GitLab MCP Server  —  Full Production Build           ║
║                                                                  ║
║  • 62 tools covering every major GitLab operation                ║
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
    "GITLAB_URL":         os.environ.get("GITLAB_URL",         "https://gitlab.com"),
    "GITLAB_TOKEN":       os.environ.get("GITLAB_TOKEN",       "glpat-dKzXW3FBozNrCV1e04uzRmM6MQpvOjEKdTptbTRnYg8.01.1707sgn0g"),
    "GITLAB_VERIFY_SSL":  os.environ.get("GITLAB_VERIFY_SSL",  "true"),
    "HOST":               os.environ.get("HOST",               "0.0.0.0"),
    "PORT":               os.environ.get("PORT",               "6502"),
    "MCP_SECRET_TOKEN":   os.environ.get("MCP_SECRET_TOKEN",   "1234456789"),
}

_missing = [k for k in ("GITLAB_TOKEN",) if not CONFIG[k]]
if _missing:
    import sys
    print(f"ERROR: Required environment variables not set: {', '.join(_missing)}")
    sys.exit(1)

# ══════════════════════════════════════════════════════════════════
#  GITLAB CLIENT
# ══════════════════════════════════════════════════════════════════

class GitLabClient:
    def __init__(self, base_url: str, token: str, verify_ssl: bool = True):
        self.api_base   = base_url.rstrip("/") + "/api/v4"
        self.token      = token
        self.verify_ssl = verify_ssl

    def _ctx(self):
        if not self.verify_ssl:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode    = ssl.CERT_NONE
            return ctx
        return None

    def _req(self, path: str, method: str = "GET", data: Optional[bytes] = None,
             content_type: str = "application/json") -> Any:
        url = f"{self.api_base}/{path.lstrip('/')}"
        headers = {
            "PRIVATE-TOKEN": self.token,
            "Content-Type":  content_type,
            "User-Agent":    "gitlab-mcp-server/1.0",
        }
        req = Request(url, data=data, headers=headers, method=method)
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

    def get(self, path):                    return self._req(path)
    def post(self, path, payload=None):
        data = json.dumps(payload or {}).encode()
        return self._req(path, "POST", data)
    def put(self, path, payload=None):
        data = json.dumps(payload or {}).encode()
        return self._req(path, "PUT", data)
    def patch(self, path, payload):
        return self._req(path, "PATCH", json.dumps(payload).encode())
    def delete(self, path):                 return self._req(path, "DELETE")

    def pid(self, project: str) -> str:
        """URL-encode a project path like 'group/repo'."""
        return quote(project, safe="")


def _client() -> GitLabClient:
    verify = CONFIG["GITLAB_VERIFY_SSL"].lower() != "false"
    return GitLabClient(CONFIG["GITLAB_URL"], CONFIG["GITLAB_TOKEN"], verify)


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


# ══════════════════════════════════════════════════════════════════
#  TOOL REGISTRY  (62 tools)
# ══════════════════════════════════════════════════════════════════

def _t(name, desc, props=None, required=None):
    schema = {"type": "object", "properties": props or {}}
    if required:
        schema["required"] = required
    return Tool(name=name, description=desc, inputSchema=schema)

def _p(type_, desc):
    return {"type": type_, "description": desc}

ALL_TOOLS = [
    # ── SERVER / USER ─────────────────────────────────────────────
    _t("get_current_user",
       "Get the currently authenticated user's profile."),

    _t("get_user",
       "Get public profile for a specific GitLab user.",
       {"username": _p("string", "GitLab username")}, ["username"]),

    _t("list_groups",
       "List groups visible to the authenticated user.",
       {"owned":     _p("boolean", "Only groups you own"),
        "search":    _p("string",  "Search by name"),
        "per_page":  _p("integer", "Results per page")}),

    _t("get_group",
       "Get details of a specific group.",
       {"group_id": _p("string", "Group ID or URL-encoded path")}, ["group_id"]),

    _t("get_version",
       "Get the GitLab server version and revision."),

    # ── PROJECTS ──────────────────────────────────────────────────
    _t("list_projects",
       "List projects accessible to the authenticated user.",
       {"membership": _p("boolean", "Only projects you're a member of"),
        "owned":      _p("boolean", "Only owned projects"),
        "search":     _p("string",  "Search by name"),
        "sort":       _p("string",  "Sort order: asc or desc"),
        "order_by":   _p("string",  "Order by: id, name, path, created_at, updated_at, last_activity_at"),
        "per_page":   _p("integer", "Results per page (max 100)")}),

    _t("get_project",
       "Get full details of a project.",
       {"project": _p("string", "Project ID or namespace/path (e.g. group/repo)")},
       ["project"]),

    _t("create_project",
       "Create a new GitLab project.",
       {"name":                  _p("string",  "Project name"),
        "namespace_id":          _p("integer", "Namespace ID (group or user, optional)"),
        "description":           _p("string",  "Description (optional)"),
        "visibility":            _p("string",  "private, internal, or public"),
        "initialize_with_readme":_p("boolean", "Add README on creation (optional)")},
       ["name"]),

    _t("update_project",
       "Update project settings.",
       {"project":        _p("string",  "Project ID or path"),
        "name":           _p("string",  "New name"),
        "description":    _p("string",  "New description"),
        "visibility":     _p("string",  "private, internal, public"),
        "default_branch": _p("string",  "Default branch"),
        "archived":       _p("boolean", "Archive/unarchive")},
       ["project"]),

    _t("delete_project",
       "Permanently delete a project. IRREVERSIBLE.",
       {"project": _p("string", "Project ID or path")}, ["project"]),

    _t("fork_project",
       "Fork a project.",
       {"project":      _p("string",  "Source project ID or path"),
        "namespace":    _p("string",  "Target namespace path (optional)"),
        "name":         _p("string",  "Name for the fork (optional)")},
       ["project"]),

    _t("list_project_members",
       "List members of a project.",
       {"project":  _p("string",  "Project ID or path"),
        "per_page": _p("integer", "Results per page")},
       ["project"]),

    _t("add_project_member",
       "Add a user to a project.",
       {"project":    _p("string",  "Project ID or path"),
        "user_id":    _p("integer", "User ID to add"),
        "access_level": _p("integer", "10=Guest 20=Reporter 30=Developer 40=Maintainer 50=Owner")},
       ["project", "user_id", "access_level"]),

    _t("remove_project_member",
       "Remove a member from a project.",
       {"project": _p("string",  "Project ID or path"),
        "user_id": _p("integer", "User ID to remove")},
       ["project", "user_id"]),

    # ── BRANCHES ──────────────────────────────────────────────────
    _t("list_branches",
       "List branches in a project.",
       {"project":  _p("string",  "Project ID or path"),
        "search":   _p("string",  "Filter by branch name"),
        "per_page": _p("integer", "Results per page")},
       ["project"]),

    _t("get_branch",
       "Get details of a specific branch.",
       {"project": _p("string", "Project ID or path"),
        "branch":  _p("string", "Branch name")},
       ["project", "branch"]),

    _t("create_branch",
       "Create a new branch.",
       {"project": _p("string", "Project ID or path"),
        "branch":  _p("string", "New branch name"),
        "ref":     _p("string", "Source branch name or commit SHA")},
       ["project", "branch", "ref"]),

    _t("delete_branch",
       "Delete a branch.",
       {"project": _p("string", "Project ID or path"),
        "branch":  _p("string", "Branch name to delete")},
       ["project", "branch"]),

    _t("protect_branch",
       "Protect a branch (restrict push/merge).",
       {"project":          _p("string",  "Project ID or path"),
        "branch":           _p("string",  "Branch name or wildcard"),
        "push_access_level":_p("integer", "Min access to push: 0=No 30=Dev 40=Maintainer"),
        "merge_access_level":_p("integer","Min access to merge: 0=No 30=Dev 40=Maintainer")},
       ["project", "branch"]),

    _t("unprotect_branch",
       "Remove branch protection.",
       {"project": _p("string", "Project ID or path"),
        "branch":  _p("string", "Branch name")},
       ["project", "branch"]),

    # ── COMMITS ───────────────────────────────────────────────────
    _t("list_commits",
       "List commits in a project branch.",
       {"project":  _p("string",  "Project ID or path"),
        "ref_name": _p("string",  "Branch/tag/SHA (optional)"),
        "since":    _p("string",  "ISO 8601 date filter (optional)"),
        "until":    _p("string",  "ISO 8601 date filter (optional)"),
        "path":     _p("string",  "Filter by file path (optional)"),
        "per_page": _p("integer", "Results per page")},
       ["project"]),

    _t("get_commit",
       "Get a specific commit.",
       {"project": _p("string", "Project ID or path"),
        "sha":     _p("string", "Commit SHA")},
       ["project", "sha"]),

    _t("get_commit_diff",
       "Get the diff for a specific commit.",
       {"project": _p("string", "Project ID or path"),
        "sha":     _p("string", "Commit SHA")},
       ["project", "sha"]),

    _t("compare_refs",
       "Compare two branches/tags/commits and return diff + commits.",
       {"project": _p("string", "Project ID or path"),
        "from_ref":_p("string", "Source ref (branch/SHA)"),
        "to_ref":  _p("string", "Target ref (branch/SHA)")},
       ["project", "from_ref", "to_ref"]),

    # ── REPOSITORY FILES ──────────────────────────────────────────
    _t("get_file",
       "Get file content from a repository.",
       {"project":  _p("string", "Project ID or path"),
        "file_path":_p("string", "File path in the repo"),
        "ref":      _p("string", "Branch/tag/SHA (default: default branch)")},
       ["project", "file_path"]),

    _t("create_file",
       "Create a new file in the repository.",
       {"project":        _p("string", "Project ID or path"),
        "file_path":      _p("string", "File path to create"),
        "branch":         _p("string", "Target branch"),
        "content":        _p("string", "File content (plain text)"),
        "commit_message": _p("string", "Commit message"),
        "author_email":   _p("string", "Author email (optional)"),
        "author_name":    _p("string", "Author name (optional)")},
       ["project", "file_path", "branch", "content", "commit_message"]),

    _t("update_file",
       "Update an existing file in the repository.",
       {"project":        _p("string", "Project ID or path"),
        "file_path":      _p("string", "File path to update"),
        "branch":         _p("string", "Target branch"),
        "content":        _p("string", "New file content"),
        "commit_message": _p("string", "Commit message"),
        "last_commit_id": _p("string", "Last known commit ID (optional, for conflict detection)")},
       ["project", "file_path", "branch", "content", "commit_message"]),

    _t("delete_file",
       "Delete a file from the repository.",
       {"project":        _p("string", "Project ID or path"),
        "file_path":      _p("string", "File path to delete"),
        "branch":         _p("string", "Target branch"),
        "commit_message": _p("string", "Commit message")},
       ["project", "file_path", "branch", "commit_message"]),

    _t("list_repository_tree",
       "List files and directories in a repository path.",
       {"project":   _p("string",  "Project ID or path"),
        "path":      _p("string",  "Directory path (default: root)"),
        "ref":       _p("string",  "Branch/tag/SHA"),
        "recursive": _p("boolean", "Recurse into subdirectories"),
        "per_page":  _p("integer", "Results per page")},
       ["project"]),

    # ── MERGE REQUESTS ────────────────────────────────────────────
    _t("list_merge_requests",
       "List merge requests for a project.",
       {"project":    _p("string",  "Project ID or path"),
        "state":      _p("string",  "State: opened, closed, locked, merged, all (default: opened)"),
        "scope":      _p("string",  "Scope: created_by_me, assigned_to_me, all"),
        "labels":     _p("string",  "Comma-separated label names"),
        "target_branch": _p("string","Filter by target branch"),
        "per_page":   _p("integer", "Results per page")},
       ["project"]),

    _t("get_merge_request",
       "Get details of a specific merge request.",
       {"project": _p("string",  "Project ID or path"),
        "mr_iid":  _p("integer", "Merge request internal ID")},
       ["project", "mr_iid"]),

    _t("create_merge_request",
       "Create a new merge request.",
       {"project":         _p("string",  "Project ID or path"),
        "source_branch":   _p("string",  "Source branch"),
        "target_branch":   _p("string",  "Target branch"),
        "title":           _p("string",  "MR title"),
        "description":     _p("string",  "MR description (optional)"),
        "assignee_id":     _p("integer", "Assignee user ID (optional)"),
        "labels":          _p("string",  "Comma-separated labels (optional)"),
        "remove_source_branch": _p("boolean", "Delete source branch on merge")},
       ["project", "source_branch", "target_branch", "title"]),

    _t("update_merge_request",
       "Update a merge request (title, description, labels, assignee, state).",
       {"project":      _p("string",  "Project ID or path"),
        "mr_iid":       _p("integer", "Merge request IID"),
        "title":        _p("string",  "New title"),
        "description":  _p("string",  "New description"),
        "state_event":  _p("string",  "close or reopen"),
        "labels":       _p("string",  "New labels (comma-separated)"),
        "assignee_id":  _p("integer", "New assignee user ID"),
        "target_branch":_p("string",  "New target branch")},
       ["project", "mr_iid"]),

    _t("merge_merge_request",
       "Accept and merge a merge request.",
       {"project":                  _p("string",  "Project ID or path"),
        "mr_iid":                   _p("integer", "Merge request IID"),
        "merge_commit_message":     _p("string",  "Custom merge commit message"),
        "squash":                   _p("boolean", "Squash commits on merge"),
        "should_remove_source_branch": _p("boolean", "Remove source branch after merge")},
       ["project", "mr_iid"]),

    _t("list_mr_notes",
       "List comments/notes on a merge request.",
       {"project": _p("string",  "Project ID or path"),
        "mr_iid":  _p("integer", "Merge request IID")},
       ["project", "mr_iid"]),

    _t("create_mr_note",
       "Add a comment to a merge request.",
       {"project": _p("string",  "Project ID or path"),
        "mr_iid":  _p("integer", "Merge request IID"),
        "body":    _p("string",  "Comment text")},
       ["project", "mr_iid", "body"]),

    _t("list_mr_approvals",
       "Get approval status of a merge request.",
       {"project": _p("string",  "Project ID or path"),
        "mr_iid":  _p("integer", "Merge request IID")},
       ["project", "mr_iid"]),

    _t("approve_merge_request",
       "Approve a merge request.",
       {"project": _p("string",  "Project ID or path"),
        "mr_iid":  _p("integer", "Merge request IID")},
       ["project", "mr_iid"]),

    # ── ISSUES ────────────────────────────────────────────────────
    _t("list_issues",
       "List issues for a project.",
       {"project":    _p("string",  "Project ID or path"),
        "state":      _p("string",  "opened, closed, or all (default: opened)"),
        "labels":     _p("string",  "Comma-separated label names"),
        "assignee_id":_p("integer", "Filter by assignee user ID"),
        "milestone":  _p("string",  "Filter by milestone title"),
        "per_page":   _p("integer", "Results per page")},
       ["project"]),

    _t("get_issue",
       "Get a specific issue.",
       {"project":   _p("string",  "Project ID or path"),
        "issue_iid": _p("integer", "Issue internal ID")},
       ["project", "issue_iid"]),

    _t("create_issue",
       "Create a new issue.",
       {"project":      _p("string",  "Project ID or path"),
        "title":        _p("string",  "Issue title"),
        "description":  _p("string",  "Issue description"),
        "labels":       _p("string",  "Comma-separated labels"),
        "assignee_ids": _p("array",   "Array of user IDs"),
        "milestone_id": _p("integer", "Milestone ID"),
        "due_date":     _p("string",  "Due date YYYY-MM-DD")},
       ["project", "title"]),

    _t("update_issue",
       "Update an existing issue.",
       {"project":     _p("string",  "Project ID or path"),
        "issue_iid":   _p("integer", "Issue IID"),
        "title":       _p("string",  "New title"),
        "description": _p("string",  "New description"),
        "state_event": _p("string",  "close or reopen"),
        "labels":      _p("string",  "New labels (comma-separated)"),
        "assignee_ids":_p("array",   "New assignee user IDs")},
       ["project", "issue_iid"]),

    _t("delete_issue",
       "Delete an issue (requires Maintainer role).",
       {"project":   _p("string",  "Project ID or path"),
        "issue_iid": _p("integer", "Issue IID")},
       ["project", "issue_iid"]),

    _t("create_issue_note",
       "Add a comment to an issue.",
       {"project":   _p("string",  "Project ID or path"),
        "issue_iid": _p("integer", "Issue IID"),
        "body":      _p("string",  "Comment text")},
       ["project", "issue_iid", "body"]),

    _t("list_issue_notes",
       "List comments on an issue.",
       {"project":   _p("string",  "Project ID or path"),
        "issue_iid": _p("integer", "Issue IID")},
       ["project", "issue_iid"]),

    # ── LABELS ────────────────────────────────────────────────────
    _t("list_labels",
       "List labels for a project.",
       {"project": _p("string", "Project ID or path")}, ["project"]),

    _t("create_label",
       "Create a label.",
       {"project":     _p("string", "Project ID or path"),
        "name":        _p("string", "Label name"),
        "color":       _p("string", "Label color (hex, e.g. #ff0000)"),
        "description": _p("string", "Description (optional)")},
       ["project", "name", "color"]),

    _t("delete_label",
       "Delete a label.",
       {"project": _p("string", "Project ID or path"),
        "name":    _p("string", "Label name")},
       ["project", "name"]),

    # ── MILESTONES ────────────────────────────────────────────────
    _t("list_milestones",
       "List milestones for a project.",
       {"project": _p("string", "Project ID or path"),
        "state":   _p("string", "active or closed (default: active)")},
       ["project"]),

    _t("create_milestone",
       "Create a milestone.",
       {"project":     _p("string", "Project ID or path"),
        "title":       _p("string", "Milestone title"),
        "description": _p("string", "Description"),
        "due_date":    _p("string", "Due date YYYY-MM-DD")},
       ["project", "title"]),

    # ── CI/CD PIPELINES ───────────────────────────────────────────
    _t("list_pipelines",
       "List CI/CD pipelines for a project.",
       {"project":  _p("string",  "Project ID or path"),
        "status":   _p("string",  "Filter: running, pending, success, failed, canceled, skipped"),
        "ref":      _p("string",  "Filter by branch or tag"),
        "per_page": _p("integer", "Results per page")},
       ["project"]),

    _t("get_pipeline",
       "Get details of a specific pipeline.",
       {"project":     _p("string",  "Project ID or path"),
        "pipeline_id": _p("integer", "Pipeline ID")},
       ["project", "pipeline_id"]),

    _t("create_pipeline",
       "Trigger a new pipeline on a branch/tag.",
       {"project":    _p("string", "Project ID or path"),
        "ref":        _p("string", "Branch or tag name"),
        "variables":  _p("array",  "Array of {key, value} objects (optional)")},
       ["project", "ref"]),

    _t("cancel_pipeline",
       "Cancel a running pipeline.",
       {"project":     _p("string",  "Project ID or path"),
        "pipeline_id": _p("integer", "Pipeline ID")},
       ["project", "pipeline_id"]),

    _t("retry_pipeline",
       "Retry a failed pipeline.",
       {"project":     _p("string",  "Project ID or path"),
        "pipeline_id": _p("integer", "Pipeline ID")},
       ["project", "pipeline_id"]),

    _t("delete_pipeline",
       "Delete a pipeline record.",
       {"project":     _p("string",  "Project ID or path"),
        "pipeline_id": _p("integer", "Pipeline ID")},
       ["project", "pipeline_id"]),

    _t("list_pipeline_jobs",
       "List jobs in a pipeline.",
       {"project":     _p("string",  "Project ID or path"),
        "pipeline_id": _p("integer", "Pipeline ID"),
        "scope":       _p("string",  "Filter: created, pending, running, failed, success, canceled, skipped, manual")},
       ["project", "pipeline_id"]),

    _t("get_job",
       "Get details of a specific CI job.",
       {"project": _p("string",  "Project ID or path"),
        "job_id":  _p("integer", "Job ID")},
       ["project", "job_id"]),

    _t("get_job_log",
       "Get the log/trace output for a CI job.",
       {"project": _p("string",  "Project ID or path"),
        "job_id":  _p("integer", "Job ID")},
       ["project", "job_id"]),

    _t("retry_job",
       "Retry a failed or cancelled CI job.",
       {"project": _p("string",  "Project ID or path"),
        "job_id":  _p("integer", "Job ID")},
       ["project", "job_id"]),

    _t("cancel_job",
       "Cancel a running CI job.",
       {"project": _p("string",  "Project ID or path"),
        "job_id":  _p("integer", "Job ID")},
       ["project", "job_id"]),

    _t("play_job",
       "Trigger a manual CI job.",
       {"project": _p("string",  "Project ID or path"),
        "job_id":  _p("integer", "Job ID")},
       ["project", "job_id"]),

    # ── RUNNERS ───────────────────────────────────────────────────
    _t("list_project_runners",
       "List runners available to a project.",
       {"project": _p("string", "Project ID or path"),
        "status":  _p("string", "Filter: active, paused, online, offline")},
       ["project"]),

    _t("list_all_runners",
       "List all runners (admin only).",
       {"status": _p("string", "Filter: active, paused, online, offline, not_connected")}),

    _t("enable_runner_for_project",
       "Enable a specific runner for a project.",
       {"project":   _p("string",  "Project ID or path"),
        "runner_id": _p("integer", "Runner ID")},
       ["project", "runner_id"]),

    _t("disable_runner_for_project",
       "Disable a runner for a project.",
       {"project":   _p("string",  "Project ID or path"),
        "runner_id": _p("integer", "Runner ID")},
       ["project", "runner_id"]),

    # ── TAGS & RELEASES ───────────────────────────────────────────
    _t("list_tags",
       "List tags for a project.",
       {"project":  _p("string",  "Project ID or path"),
        "per_page": _p("integer", "Results per page")},
       ["project"]),

    _t("create_tag",
       "Create a new tag.",
       {"project": _p("string", "Project ID or path"),
        "tag_name":_p("string", "Tag name"),
        "ref":     _p("string", "Commit SHA or branch to tag"),
        "message": _p("string", "Tag message (creates annotated tag if set)")},
       ["project", "tag_name", "ref"]),

    _t("delete_tag",
       "Delete a tag.",
       {"project":  _p("string", "Project ID or path"),
        "tag_name": _p("string", "Tag name to delete")},
       ["project", "tag_name"]),

    _t("create_release",
       "Create a release for a tag.",
       {"project":     _p("string", "Project ID or path"),
        "tag_name":    _p("string", "Tag name"),
        "name":        _p("string", "Release name"),
        "description": _p("string", "Release notes (markdown)")},
       ["project", "tag_name", "name"]),

    # ── WEBHOOKS ──────────────────────────────────────────────────
    _t("list_project_hooks",
       "List webhooks for a project.",
       {"project": _p("string", "Project ID or path")}, ["project"]),

    _t("create_project_hook",
       "Create a webhook for a project.",
       {"project":               _p("string",  "Project ID or path"),
        "url":                   _p("string",  "Webhook URL"),
        "push_events":           _p("boolean", "Trigger on push (default true)"),
        "merge_requests_events": _p("boolean", "Trigger on MR events"),
        "issues_events":         _p("boolean", "Trigger on issue events"),
        "pipeline_events":       _p("boolean", "Trigger on pipeline events"),
        "token":                 _p("string",  "Secret token for verification"),
        "enable_ssl_verification":_p("boolean","Enable SSL verification (default true)")},
       ["project", "url"]),

    _t("delete_project_hook",
       "Delete a project webhook.",
       {"project": _p("string",  "Project ID or path"),
        "hook_id": _p("integer", "Hook ID")},
       ["project", "hook_id"]),

    # ── SEARCH ────────────────────────────────────────────────────
    _t("search_projects",
       "Search for projects by name.",
       {"query":    _p("string",  "Search query"),
        "per_page": _p("integer", "Results per page")},
       ["query"]),

    _t("search_in_project",
       "Search within a project (code, issues, merge_requests, commits, blobs).",
       {"project": _p("string",  "Project ID or path"),
        "scope":   _p("string",  "blobs, commits, issues, merge_requests, milestones, notes, wiki_blobs"),
        "query":   _p("string",  "Search query")},
       ["project", "scope", "query"]),

    # ── VARIABLES / SECRETS ───────────────────────────────────────
    _t("list_project_variables",
       "List CI/CD variables for a project.",
       {"project": _p("string", "Project ID or path")}, ["project"]),

    _t("create_project_variable",
       "Create a CI/CD variable.",
       {"project":    _p("string",  "Project ID or path"),
        "key":        _p("string",  "Variable key"),
        "value":      _p("string",  "Variable value"),
        "protected":  _p("boolean", "Only exposed on protected branches"),
        "masked":     _p("boolean", "Mask value in logs"),
        "variable_type": _p("string", "env_var or file (default: env_var)")},
       ["project", "key", "value"]),

    _t("update_project_variable",
       "Update a CI/CD variable.",
       {"project":   _p("string",  "Project ID or path"),
        "key":       _p("string",  "Variable key"),
        "value":     _p("string",  "New value"),
        "protected": _p("boolean", "Protected flag"),
        "masked":    _p("boolean", "Mask in logs")},
       ["project", "key", "value"]),

    _t("delete_project_variable",
       "Delete a CI/CD variable.",
       {"project": _p("string", "Project ID or path"),
        "key":     _p("string", "Variable key")},
       ["project", "key"]),
]

# ══════════════════════════════════════════════════════════════════
#  TOOL DISPATCHER
# ══════════════════════════════════════════════════════════════════

import base64 as _b64

async def call_tool(name: str, args: dict) -> CTR:
    try:
        c = _client()
    except RuntimeError as e:
        return err(str(e))
    try:
        return await _dispatch(name, args, c)
    except Exception as e:
        return err(str(e))


async def _dispatch(name: str, a: dict, c: GitLabClient) -> CTR:
    p = lambda: c.pid(a["project"])

    # ── SERVER / USER ─────────────────────────────────────────────
    if name == "get_current_user":
        return ok(c.get("user"))

    if name == "get_user":
        return ok(c.get(f"users?username={a['username']}"))

    if name == "list_groups":
        params = []
        if a.get("owned"):    params.append("owned=true")
        if a.get("search"):   params.append(f"search={a['search']}")
        if a.get("per_page"): params.append(f"per_page={a['per_page']}")
        qs = "?" + "&".join(params) if params else ""
        return ok(c.get(f"groups{qs}"))

    if name == "get_group":
        return ok(c.get(f"groups/{a['group_id']}"))

    if name == "get_version":
        return ok(c.get("version"))

    # ── PROJECTS ──────────────────────────────────────────────────
    if name == "list_projects":
        params = []
        for k in ("membership", "owned", "search", "sort", "order_by", "per_page"):
            v = a.get(k)
            if v is not None: params.append(f"{k}={v}")
        qs = "?" + "&".join(params) if params else ""
        return ok(c.get(f"projects{qs}"))

    if name == "get_project":
        return ok(c.get(f"projects/{p()}"))

    if name == "create_project":
        payload = {"name": a["name"]}
        for k in ("namespace_id", "description", "visibility", "initialize_with_readme"):
            if a.get(k) is not None: payload[k] = a[k]
        return ok(c.post("projects", payload))

    if name == "update_project":
        payload = {k: v for k, v in {
            "name": a.get("name"), "description": a.get("description"),
            "visibility": a.get("visibility"), "default_branch": a.get("default_branch"),
            "archived": a.get("archived"),
        }.items() if v is not None}
        return ok(c.put(f"projects/{p()}", payload))

    if name == "delete_project":
        c.delete(f"projects/{p()}")
        return ok(f"Project '{a['project']}' deleted.")

    if name == "fork_project":
        payload = {}
        if a.get("namespace"): payload["namespace"] = a["namespace"]
        if a.get("name"):      payload["name"]      = a["name"]
        return ok(c.post(f"projects/{p()}/fork", payload))

    if name == "list_project_members":
        pp = a.get("per_page", 30)
        return ok(c.get(f"projects/{p()}/members?per_page={pp}"))

    if name == "add_project_member":
        return ok(c.post(f"projects/{p()}/members",
                         {"user_id": a["user_id"], "access_level": a["access_level"]}))

    if name == "remove_project_member":
        c.delete(f"projects/{p()}/members/{a['user_id']}")
        return ok(f"User {a['user_id']} removed from project.")

    # ── BRANCHES ──────────────────────────────────────────────────
    if name == "list_branches":
        params = []
        if a.get("search"):   params.append(f"search={a['search']}")
        if a.get("per_page"): params.append(f"per_page={a['per_page']}")
        qs = "?" + "&".join(params) if params else ""
        return ok(c.get(f"projects/{p()}/repository/branches{qs}"))

    if name == "get_branch":
        return ok(c.get(f"projects/{p()}/repository/branches/{quote(a['branch'], safe='')}"))

    if name == "create_branch":
        return ok(c.post(f"projects/{p()}/repository/branches",
                         {"branch": a["branch"], "ref": a["ref"]}))

    if name == "delete_branch":
        c.delete(f"projects/{p()}/repository/branches/{quote(a['branch'], safe='')}")
        return ok(f"Branch '{a['branch']}' deleted.")

    if name == "protect_branch":
        payload = {"name": a["branch"]}
        if a.get("push_access_level") is not None:  payload["push_access_level"]  = a["push_access_level"]
        if a.get("merge_access_level") is not None: payload["merge_access_level"] = a["merge_access_level"]
        return ok(c.post(f"projects/{p()}/protected_branches", payload))

    if name == "unprotect_branch":
        c.delete(f"projects/{p()}/protected_branches/{quote(a['branch'], safe='')}")
        return ok(f"Branch '{a['branch']}' unprotected.")

    # ── COMMITS ───────────────────────────────────────────────────
    if name == "list_commits":
        params = []
        for k in ("ref_name", "since", "until", "path", "per_page"):
            if a.get(k): params.append(f"{k}={a[k]}")
        qs = "?" + "&".join(params) if params else ""
        return ok(c.get(f"projects/{p()}/repository/commits{qs}"))

    if name == "get_commit":
        return ok(c.get(f"projects/{p()}/repository/commits/{a['sha']}"))

    if name == "get_commit_diff":
        return ok(c.get(f"projects/{p()}/repository/commits/{a['sha']}/diff"))

    if name == "compare_refs":
        return ok(c.get(f"projects/{p()}/repository/compare?from={a['from_ref']}&to={a['to_ref']}"))

    # ── REPOSITORY FILES ──────────────────────────────────────────
    if name == "get_file":
        fp  = quote(a["file_path"], safe="")
        ref = a.get("ref", "HEAD")
        data = c.get(f"projects/{p()}/repository/files/{fp}?ref={ref}")
        if isinstance(data, dict) and data.get("encoding") == "base64":
            data["content_decoded"] = _b64.b64decode(data["content"]).decode("utf-8", errors="replace")
        return ok(data)

    if name == "create_file":
        fp      = quote(a["file_path"], safe="")
        payload = {
            "branch":         a["branch"],
            "content":        a["content"],
            "commit_message": a["commit_message"],
        }
        if a.get("author_email"): payload["author_email"] = a["author_email"]
        if a.get("author_name"):  payload["author_name"]  = a["author_name"]
        return ok(c.post(f"projects/{p()}/repository/files/{fp}", payload))

    if name == "update_file":
        fp      = quote(a["file_path"], safe="")
        payload = {
            "branch":         a["branch"],
            "content":        a["content"],
            "commit_message": a["commit_message"],
        }
        if a.get("last_commit_id"): payload["last_commit_id"] = a["last_commit_id"]
        return ok(c.put(f"projects/{p()}/repository/files/{fp}", payload))

    if name == "delete_file":
        fp      = quote(a["file_path"], safe="")
        payload = {"branch": a["branch"], "commit_message": a["commit_message"]}
        import ssl as _ssl
        url     = f"{c.api_base}/projects/{p()}/repository/files/{fp}"
        headers = {"PRIVATE-TOKEN": c.token, "Content-Type": "application/json"}
        req     = Request(url, data=json.dumps(payload).encode(), headers=headers, method="DELETE")
        with urlopen(req, context=c._ctx(), timeout=30) as r:
            return ok(r.read().decode() or f"File '{a['file_path']}' deleted.")

    if name == "list_repository_tree":
        params = []
        if a.get("path"):      params.append(f"path={a['path']}")
        if a.get("ref"):       params.append(f"ref={a['ref']}")
        if a.get("recursive"): params.append("recursive=true")
        if a.get("per_page"):  params.append(f"per_page={a['per_page']}")
        qs = "?" + "&".join(params) if params else ""
        return ok(c.get(f"projects/{p()}/repository/tree{qs}"))

    # ── MERGE REQUESTS ────────────────────────────────────────────
    if name == "list_merge_requests":
        params = [f"state={a.get('state', 'opened')}"]
        for k in ("scope", "labels", "target_branch", "per_page"):
            if a.get(k): params.append(f"{k}={a[k]}")
        return ok(c.get(f"projects/{p()}/merge_requests?{'&'.join(params)}"))

    if name == "get_merge_request":
        return ok(c.get(f"projects/{p()}/merge_requests/{a['mr_iid']}"))

    if name == "create_merge_request":
        payload = {
            "source_branch": a["source_branch"],
            "target_branch": a["target_branch"],
            "title":         a["title"],
        }
        for k in ("description", "assignee_id", "labels", "remove_source_branch"):
            if a.get(k) is not None: payload[k] = a[k]
        return ok(c.post(f"projects/{p()}/merge_requests", payload))

    if name == "update_merge_request":
        payload = {k: v for k, v in {
            "title": a.get("title"), "description": a.get("description"),
            "state_event": a.get("state_event"), "labels": a.get("labels"),
            "assignee_id": a.get("assignee_id"), "target_branch": a.get("target_branch"),
        }.items() if v is not None}
        return ok(c.put(f"projects/{p()}/merge_requests/{a['mr_iid']}", payload))

    if name == "merge_merge_request":
        payload = {}
        for k in ("merge_commit_message", "squash", "should_remove_source_branch"):
            if a.get(k) is not None: payload[k] = a[k]
        return ok(c.put(f"projects/{p()}/merge_requests/{a['mr_iid']}/merge", payload))

    if name == "list_mr_notes":
        return ok(c.get(f"projects/{p()}/merge_requests/{a['mr_iid']}/notes"))

    if name == "create_mr_note":
        return ok(c.post(f"projects/{p()}/merge_requests/{a['mr_iid']}/notes", {"body": a["body"]}))

    if name == "list_mr_approvals":
        return ok(c.get(f"projects/{p()}/merge_requests/{a['mr_iid']}/approvals"))

    if name == "approve_merge_request":
        return ok(c.post(f"projects/{p()}/merge_requests/{a['mr_iid']}/approve"))

    # ── ISSUES ────────────────────────────────────────────────────
    if name == "list_issues":
        params = [f"state={a.get('state', 'opened')}"]
        for k in ("labels", "assignee_id", "milestone", "per_page"):
            if a.get(k) is not None: params.append(f"{k}={a[k]}")
        return ok(c.get(f"projects/{p()}/issues?{'&'.join(params)}"))

    if name == "get_issue":
        return ok(c.get(f"projects/{p()}/issues/{a['issue_iid']}"))

    if name == "create_issue":
        payload = {"title": a["title"]}
        for k in ("description", "labels", "assignee_ids", "milestone_id", "due_date"):
            if a.get(k) is not None: payload[k] = a[k]
        return ok(c.post(f"projects/{p()}/issues", payload))

    if name == "update_issue":
        payload = {k: v for k, v in {
            "title": a.get("title"), "description": a.get("description"),
            "state_event": a.get("state_event"), "labels": a.get("labels"),
            "assignee_ids": a.get("assignee_ids"),
        }.items() if v is not None}
        return ok(c.put(f"projects/{p()}/issues/{a['issue_iid']}", payload))

    if name == "delete_issue":
        c.delete(f"projects/{p()}/issues/{a['issue_iid']}")
        return ok(f"Issue {a['issue_iid']} deleted.")

    if name == "create_issue_note":
        return ok(c.post(f"projects/{p()}/issues/{a['issue_iid']}/notes", {"body": a["body"]}))

    if name == "list_issue_notes":
        return ok(c.get(f"projects/{p()}/issues/{a['issue_iid']}/notes"))

    # ── LABELS ────────────────────────────────────────────────────
    if name == "list_labels":
        return ok(c.get(f"projects/{p()}/labels"))

    if name == "create_label":
        payload = {"name": a["name"], "color": a["color"]}
        if a.get("description"): payload["description"] = a["description"]
        return ok(c.post(f"projects/{p()}/labels", payload))

    if name == "delete_label":
        c.delete(f"projects/{p()}/labels?name={quote(a['name'])}")
        return ok(f"Label '{a['name']}' deleted.")

    # ── MILESTONES ────────────────────────────────────────────────
    if name == "list_milestones":
        state = a.get("state", "active")
        return ok(c.get(f"projects/{p()}/milestones?state={state}"))

    if name == "create_milestone":
        payload = {"title": a["title"]}
        if a.get("description"): payload["description"] = a["description"]
        if a.get("due_date"):    payload["due_date"]    = a["due_date"]
        return ok(c.post(f"projects/{p()}/milestones", payload))

    # ── CI/CD PIPELINES ───────────────────────────────────────────
    if name == "list_pipelines":
        params = []
        for k in ("status", "ref", "per_page"):
            if a.get(k): params.append(f"{k}={a[k]}")
        qs = "?" + "&".join(params) if params else ""
        return ok(c.get(f"projects/{p()}/pipelines{qs}"))

    if name == "get_pipeline":
        return ok(c.get(f"projects/{p()}/pipelines/{a['pipeline_id']}"))

    if name == "create_pipeline":
        payload = {"ref": a["ref"]}
        if a.get("variables"): payload["variables"] = a["variables"]
        return ok(c.post(f"projects/{p()}/pipeline", payload))

    if name == "cancel_pipeline":
        return ok(c.post(f"projects/{p()}/pipelines/{a['pipeline_id']}/cancel"))

    if name == "retry_pipeline":
        return ok(c.post(f"projects/{p()}/pipelines/{a['pipeline_id']}/retry"))

    if name == "delete_pipeline":
        c.delete(f"projects/{p()}/pipelines/{a['pipeline_id']}")
        return ok(f"Pipeline {a['pipeline_id']} deleted.")

    if name == "list_pipeline_jobs":
        qs = f"?scope={a['scope']}" if a.get("scope") else ""
        return ok(c.get(f"projects/{p()}/pipelines/{a['pipeline_id']}/jobs{qs}"))

    if name == "get_job":
        return ok(c.get(f"projects/{p()}/jobs/{a['job_id']}"))

    if name == "get_job_log":
        return ok(c.get(f"projects/{p()}/jobs/{a['job_id']}/trace"))

    if name == "retry_job":
        return ok(c.post(f"projects/{p()}/jobs/{a['job_id']}/retry"))

    if name == "cancel_job":
        return ok(c.post(f"projects/{p()}/jobs/{a['job_id']}/cancel"))

    if name == "play_job":
        return ok(c.post(f"projects/{p()}/jobs/{a['job_id']}/play"))

    # ── RUNNERS ───────────────────────────────────────────────────
    if name == "list_project_runners":
        qs = f"?status={a['status']}" if a.get("status") else ""
        return ok(c.get(f"projects/{p()}/runners{qs}"))

    if name == "list_all_runners":
        qs = f"?status={a['status']}" if a.get("status") else ""
        return ok(c.get(f"runners/all{qs}"))

    if name == "enable_runner_for_project":
        return ok(c.post(f"projects/{p()}/runners", {"runner_id": a["runner_id"]}))

    if name == "disable_runner_for_project":
        c.delete(f"projects/{p()}/runners/{a['runner_id']}")
        return ok(f"Runner {a['runner_id']} disabled for project.")

    # ── TAGS & RELEASES ───────────────────────────────────────────
    if name == "list_tags":
        pp = a.get("per_page", 30)
        return ok(c.get(f"projects/{p()}/repository/tags?per_page={pp}"))

    if name == "create_tag":
        payload = {"tag_name": a["tag_name"], "ref": a["ref"]}
        if a.get("message"): payload["message"] = a["message"]
        return ok(c.post(f"projects/{p()}/repository/tags", payload))

    if name == "delete_tag":
        c.delete(f"projects/{p()}/repository/tags/{quote(a['tag_name'], safe='')}")
        return ok(f"Tag '{a['tag_name']}' deleted.")

    if name == "create_release":
        payload = {"name": a["name"]}
        if a.get("description"): payload["description"] = a["description"]
        return ok(c.post(f"projects/{p()}/releases", {
            "tag_name": a["tag_name"], "name": a["name"],
            "description": a.get("description", ""),
        }))

    # ── WEBHOOKS ──────────────────────────────────────────────────
    if name == "list_project_hooks":
        return ok(c.get(f"projects/{p()}/hooks"))

    if name == "create_project_hook":
        payload = {"url": a["url"], "push_events": a.get("push_events", True)}
        for k in ("merge_requests_events", "issues_events", "pipeline_events",
                  "token", "enable_ssl_verification"):
            if a.get(k) is not None: payload[k] = a[k]
        return ok(c.post(f"projects/{p()}/hooks", payload))

    if name == "delete_project_hook":
        c.delete(f"projects/{p()}/hooks/{a['hook_id']}")
        return ok(f"Webhook {a['hook_id']} deleted.")

    # ── SEARCH ────────────────────────────────────────────────────
    if name == "search_projects":
        pp = a.get("per_page", 20)
        return ok(c.get(f"projects?search={quote(a['query'])}&per_page={pp}"))

    if name == "search_in_project":
        return ok(c.get(f"projects/{p()}/search?scope={a['scope']}&search={quote(a['query'])}"))

    # ── VARIABLES ─────────────────────────────────────────────────
    if name == "list_project_variables":
        return ok(c.get(f"projects/{p()}/variables"))

    if name == "create_project_variable":
        payload = {"key": a["key"], "value": a["value"]}
        for k in ("protected", "masked", "variable_type"):
            if a.get(k) is not None: payload[k] = a[k]
        return ok(c.post(f"projects/{p()}/variables", payload))

    if name == "update_project_variable":
        payload = {"value": a["value"]}
        for k in ("protected", "masked"):
            if a.get(k) is not None: payload[k] = a[k]
        return ok(c.put(f"projects/{p()}/variables/{a['key']}", payload))

    if name == "delete_project_variable":
        c.delete(f"projects/{p()}/variables/{a['key']}")
        return ok(f"Variable '{a['key']}' deleted.")

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
        "status": "ok", "server": "gitlab-mcp",
        "tools": len(ALL_TOOLS), "url": CONFIG["GITLAB_URL"],
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
                                "serverInfo": {"name": "gitlab-mcp", "version": "1.0.0"}})
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
                                "serverInfo": {"name": "gitlab-mcp", "version": "1.0.0"}})
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
    print(f"║       GitLab MCP Server — Running            ║")
    print(f"╠══════════════════════════════════════════════╣")
    print(f"║  POST  http://{host}:{port}/mcp")
    print(f"║  GET   http://{host}:{port}/mcp  (SSE)")
    print(f"║  GET   http://{host}:{port}/sse  (legacy)")
    print(f"║  Tools : {len(ALL_TOOLS)}")
    print(f"║  URL   : {CONFIG['GITLAB_URL']}")
    print(f"╚══════════════════════════════════════════════╝")
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
