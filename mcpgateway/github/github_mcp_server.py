
#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║           GitHub MCP Server  —  Full Production Build           ║
║                                                                  ║
║  • 60 tools covering every major GitHub operation                ║
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
    "GITHUB_URL":          os.environ.get("GITHUB_URL",          "https://api.github.com"),
    "GITHUB_PAT":          os.environ.get("GITHUB_PAT",          "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"),
    "GITHUB_ORG":          os.environ.get("GITHUB_ORG",          "ai-cicd-bots"),
    "GITHUB_VERIFY_SSL":   os.environ.get("GITHUB_VERIFY_SSL",   "true"),
    "HOST":                os.environ.get("HOST",                 "0.0.0.0"),
    "PORT":                os.environ.get("PORT",                 "6501"),
    "MCP_SECRET_TOKEN":    os.environ.get("MCP_SECRET_TOKEN",    "1234456789"),
}

_missing = [k for k in ("GITHUB_PAT",) if not CONFIG[k]]
if _missing:
    import sys
    print(f"ERROR: Required environment variables not set: {', '.join(_missing)}")
    sys.exit(1)

# ══════════════════════════════════════════════════════════════════
#  GITHUB CLIENT
# ══════════════════════════════════════════════════════════════════

class GitHubClient:
    def __init__(self, base_url: str, pat: str, verify_ssl: bool = True):
        self.base_url   = base_url.rstrip("/")
        self.pat        = pat
        self.verify_ssl = verify_ssl

    def _ctx(self):
        if not self.verify_ssl:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode    = ssl.CERT_NONE
            return ctx
        return None

    def _req(self, path: str, method: str = "GET", data: Optional[bytes] = None,
             extra_headers: dict = None) -> Any:
        if path.startswith("http"):
            url = path
        else:
            url = f"{self.base_url}/{path.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {self.pat}",
            "Accept":        "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type":  "application/json",
            "User-Agent":    "github-mcp-server/1.0",
        }
        if extra_headers:
            headers.update(extra_headers)
        req = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(req, context=self._ctx(), timeout=30) as r:
                body = r.read()
                return json.loads(body) if body else {}
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code} {e.reason}: {body[:500]}")

    def get(self, path):
        return self._req(path)

    def post(self, path, payload=None):
        data = json.dumps(payload or {}).encode() if payload is not None else b""
        return self._req(path, "POST", data)

    def patch(self, path, payload):
        return self._req(path, "PATCH", json.dumps(payload).encode())

    def put(self, path, payload=None):
        data = json.dumps(payload or {}).encode()
        return self._req(path, "PUT", data)

    def delete(self, path):
        return self._req(path, "DELETE")

    def paginate(self, path, max_items=100):
        """Fetch up to max_items across pages."""
        sep    = "&" if "?" in path else "?"
        url    = f"{path}{sep}per_page=100&page=1"
        result = []
        page   = 1
        while len(result) < max_items:
            sep  = "&" if "?" in url else "?"
            data = self._req(f"{path}{sep}per_page=100&page={page}")
            if not data:
                break
            items = data if isinstance(data, list) else data.get("items", [])
            result.extend(items)
            if len(items) < 100:
                break
            page += 1
        return result[:max_items]


def _client() -> GitHubClient:
    verify = CONFIG["GITHUB_VERIFY_SSL"].lower() != "false"
    return GitHubClient(CONFIG["GITHUB_URL"], CONFIG["GITHUB_PAT"], verify)


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
#  TOOL REGISTRY  (60 tools)
# ══════════════════════════════════════════════════════════════════

def _t(name, desc, props=None, required=None):
    schema = {"type": "object", "properties": props or {}}
    if required:
        schema["required"] = required
    return Tool(name=name, description=desc, inputSchema=schema)

def _p(type_, desc):
    return {"type": type_, "description": desc}

ALL_TOOLS = [
    # ── USER / AUTH ───────────────────────────────────────────────
    _t("get_authenticated_user",
       "Get details of the authenticated user (PAT owner)."),

    _t("list_my_repos",
       "List repositories for the authenticated user.",
       {"type":      _p("string",  "Filter: all, owner, member (default: owner)"),
        "sort":      _p("string",  "Sort by: created, updated, pushed, full_name"),
        "per_page":  _p("integer", "Results per page (max 100)")}),

    _t("get_user",
       "Get public profile info for any GitHub user.",
       {"username": _p("string", "GitHub username")}, ["username"]),

    _t("list_user_repos",
       "List public repositories for a specific user.",
       {"username": _p("string", "GitHub username"),
        "per_page": _p("integer", "Results per page")},
       ["username"]),

    # ── REPOSITORIES ──────────────────────────────────────────────
    _t("get_repo",
       "Get detailed information about a repository.",
       {"owner": _p("string", "Repo owner (user or org)"),
        "repo":  _p("string", "Repository name")},
       ["owner", "repo"]),

    _t("create_repo",
       "Create a new repository for the authenticated user.",
       {"name":        _p("string",  "Repository name"),
        "description": _p("string",  "Description (optional)"),
        "private":     _p("boolean", "Make repo private (default false)"),
        "auto_init":   _p("boolean", "Initialize with README (default false)")},
       ["name"]),

    _t("update_repo",
       "Update repository metadata (description, visibility, settings).",
       {"owner":        _p("string",  "Repo owner"),
        "repo":         _p("string",  "Repository name"),
        "description":  _p("string",  "New description"),
        "private":      _p("boolean", "Set private/public"),
        "has_issues":   _p("boolean", "Enable issues"),
        "has_wiki":     _p("boolean", "Enable wiki"),
        "default_branch": _p("string", "Default branch name")},
       ["owner", "repo"]),

    _t("delete_repo",
       "Permanently delete a repository. IRREVERSIBLE.",
       {"owner": _p("string", "Repo owner"),
        "repo":  _p("string", "Repository name")},
       ["owner", "repo"]),

    _t("fork_repo",
       "Fork a repository to the authenticated user or an organization.",
       {"owner":        _p("string", "Source repo owner"),
        "repo":         _p("string", "Source repo name"),
        "organization": _p("string", "Target org (optional, defaults to your account)")},
       ["owner", "repo"]),

    _t("list_repo_topics",
       "List topics (tags) associated with a repository.",
       {"owner": _p("string", "Repo owner"),
        "repo":  _p("string", "Repository name")},
       ["owner", "repo"]),

    _t("replace_repo_topics",
       "Replace all topics for a repository.",
       {"owner":  _p("string", "Repo owner"),
        "repo":   _p("string", "Repository name"),
        "topics": _p("array",  "Array of topic strings")},
       ["owner", "repo", "topics"]),

    # ── BRANCHES & REFS ───────────────────────────────────────────
    _t("list_branches",
       "List branches of a repository.",
       {"owner":     _p("string",  "Repo owner"),
        "repo":      _p("string",  "Repository name"),
        "protected": _p("boolean", "Only protected branches")},
       ["owner", "repo"]),

    _t("get_branch",
       "Get details about a specific branch.",
       {"owner":  _p("string", "Repo owner"),
        "repo":   _p("string", "Repository name"),
        "branch": _p("string", "Branch name")},
       ["owner", "repo", "branch"]),

    _t("create_branch",
       "Create a new branch from an existing ref (branch or SHA).",
       {"owner":     _p("string", "Repo owner"),
        "repo":      _p("string", "Repository name"),
        "new_branch":_p("string", "New branch name"),
        "from_ref":  _p("string", "Source branch name or commit SHA")},
       ["owner", "repo", "new_branch", "from_ref"]),

    _t("delete_branch",
       "Delete a branch from a repository.",
       {"owner":  _p("string", "Repo owner"),
        "repo":   _p("string", "Repository name"),
        "branch": _p("string", "Branch name to delete")},
       ["owner", "repo", "branch"]),

    _t("merge_branches",
       "Merge a branch into a base branch.",
       {"owner":          _p("string", "Repo owner"),
        "repo":           _p("string", "Repository name"),
        "base":           _p("string", "Base branch to merge into"),
        "head":           _p("string", "Branch or SHA to merge"),
        "commit_message": _p("string", "Merge commit message (optional)")},
       ["owner", "repo", "base", "head"]),

    # ── COMMITS ───────────────────────────────────────────────────
    _t("list_commits",
       "List commits on a repository, optionally filtered by branch/path/author.",
       {"owner":   _p("string",  "Repo owner"),
        "repo":    _p("string",  "Repository name"),
        "sha":     _p("string",  "Branch or SHA (optional)"),
        "path":    _p("string",  "File path filter (optional)"),
        "author":  _p("string",  "Author username filter (optional)"),
        "per_page":_p("integer", "Results per page (max 100)")},
       ["owner", "repo"]),

    _t("get_commit",
       "Get details of a specific commit including file diffs.",
       {"owner":  _p("string", "Repo owner"),
        "repo":   _p("string", "Repository name"),
        "ref":    _p("string", "Commit SHA")},
       ["owner", "repo", "ref"]),

    _t("compare_commits",
       "Compare two commits or branches — shows diff and commits between them.",
       {"owner":  _p("string", "Repo owner"),
        "repo":   _p("string", "Repository name"),
        "base":   _p("string", "Base commit/branch"),
        "head":   _p("string", "Head commit/branch")},
       ["owner", "repo", "base", "head"]),

    # ── CONTENTS / FILES ──────────────────────────────────────────
    _t("get_file_contents",
       "Get the content of a file in a repository (base64 decoded).",
       {"owner": _p("string", "Repo owner"),
        "repo":  _p("string", "Repository name"),
        "path":  _p("string", "File path in the repo"),
        "ref":   _p("string", "Branch/tag/SHA (optional, defaults to default branch)")},
       ["owner", "repo", "path"]),

    _t("create_or_update_file",
       "Create or update a file in a repository.",
       {"owner":   _p("string", "Repo owner"),
        "repo":    _p("string", "Repository name"),
        "path":    _p("string", "File path in the repo"),
        "message": _p("string", "Commit message"),
        "content": _p("string", "File content (plain text, will be base64 encoded)"),
        "sha":     _p("string", "Blob SHA of file being replaced (required for updates)"),
        "branch":  _p("string", "Branch (optional)")},
       ["owner", "repo", "path", "message", "content"]),

    _t("delete_file",
       "Delete a file from a repository.",
       {"owner":   _p("string", "Repo owner"),
        "repo":    _p("string", "Repository name"),
        "path":    _p("string", "File path to delete"),
        "message": _p("string", "Commit message"),
        "sha":     _p("string", "Blob SHA of the file"),
        "branch":  _p("string", "Branch (optional)")},
       ["owner", "repo", "path", "message", "sha"]),

    _t("list_repo_contents",
       "List files and directories at a path in a repository.",
       {"owner": _p("string", "Repo owner"),
        "repo":  _p("string", "Repository name"),
        "path":  _p("string", "Directory path (default: root)"),
        "ref":   _p("string", "Branch/tag/SHA (optional)")},
       ["owner", "repo"]),

    # ── PULL REQUESTS ─────────────────────────────────────────────
    _t("list_pull_requests",
       "List pull requests for a repository.",
       {"owner":    _p("string",  "Repo owner"),
        "repo":     _p("string",  "Repository name"),
        "state":    _p("string",  "State: open, closed, all (default: open)"),
        "base":     _p("string",  "Filter by base branch"),
        "sort":     _p("string",  "Sort by: created, updated, popularity, long-running"),
        "per_page": _p("integer", "Results per page")},
       ["owner", "repo"]),

    _t("get_pull_request",
       "Get details of a specific pull request.",
       {"owner":  _p("string",  "Repo owner"),
        "repo":   _p("string",  "Repository name"),
        "pr_number": _p("integer", "Pull request number")},
       ["owner", "repo", "pr_number"]),

    _t("create_pull_request",
       "Create a new pull request.",
       {"owner":                _p("string",  "Repo owner"),
        "repo":                 _p("string",  "Repository name"),
        "title":                _p("string",  "PR title"),
        "head":                 _p("string",  "Branch with your changes"),
        "base":                 _p("string",  "Branch to merge into"),
        "body":                 _p("string",  "PR description (optional)"),
        "draft":                _p("boolean", "Mark as draft (optional)"),
        "maintainer_can_modify":_p("boolean", "Allow maintainer edits (optional)")},
       ["owner", "repo", "title", "head", "base"]),

    _t("update_pull_request",
       "Update an existing pull request (title, body, state, base branch).",
       {"owner":     _p("string",  "Repo owner"),
        "repo":      _p("string",  "Repository name"),
        "pr_number": _p("integer", "PR number"),
        "title":     _p("string",  "New title"),
        "body":      _p("string",  "New description"),
        "state":     _p("string",  "State: open or closed"),
        "base":      _p("string",  "New base branch")},
       ["owner", "repo", "pr_number"]),

    _t("merge_pull_request",
       "Merge a pull request.",
       {"owner":          _p("string",  "Repo owner"),
        "repo":           _p("string",  "Repository name"),
        "pr_number":      _p("integer", "PR number"),
        "merge_method":   _p("string",  "Method: merge, squash, rebase (default: merge)"),
        "commit_title":   _p("string",  "Commit title (optional)"),
        "commit_message": _p("string",  "Commit message (optional)")},
       ["owner", "repo", "pr_number"]),

    _t("list_pr_reviews",
       "List reviews submitted on a pull request.",
       {"owner":     _p("string",  "Repo owner"),
        "repo":      _p("string",  "Repository name"),
        "pr_number": _p("integer", "PR number")},
       ["owner", "repo", "pr_number"]),

    _t("create_pr_review",
       "Submit a review on a pull request.",
       {"owner":     _p("string",  "Repo owner"),
        "repo":      _p("string",  "Repository name"),
        "pr_number": _p("integer", "PR number"),
        "event":     _p("string",  "APPROVE, REQUEST_CHANGES, or COMMENT"),
        "body":      _p("string",  "Review comment body")},
       ["owner", "repo", "pr_number", "event"]),

    _t("list_pr_files",
       "List files changed in a pull request.",
       {"owner":     _p("string",  "Repo owner"),
        "repo":      _p("string",  "Repository name"),
        "pr_number": _p("integer", "PR number")},
       ["owner", "repo", "pr_number"]),

    # ── ISSUES ────────────────────────────────────────────────────
    _t("list_issues",
       "List issues for a repository.",
       {"owner":     _p("string",  "Repo owner"),
        "repo":      _p("string",  "Repository name"),
        "state":     _p("string",  "State: open, closed, all (default: open)"),
        "labels":    _p("string",  "Comma-separated label names"),
        "assignee":  _p("string",  "Username of assignee"),
        "sort":      _p("string",  "Sort by: created, updated, comments"),
        "per_page":  _p("integer", "Results per page")},
       ["owner", "repo"]),

    _t("get_issue",
       "Get a specific issue.",
       {"owner":        _p("string",  "Repo owner"),
        "repo":         _p("string",  "Repository name"),
        "issue_number": _p("integer", "Issue number")},
       ["owner", "repo", "issue_number"]),

    _t("create_issue",
       "Create a new issue.",
       {"owner":    _p("string", "Repo owner"),
        "repo":     _p("string", "Repository name"),
        "title":    _p("string", "Issue title"),
        "body":     _p("string", "Issue body (optional)"),
        "labels":   _p("array",  "Array of label names (optional)"),
        "assignees":_p("array",  "Array of usernames (optional)"),
        "milestone":_p("integer","Milestone number (optional)")},
       ["owner", "repo", "title"]),

    _t("update_issue",
       "Update an existing issue.",
       {"owner":        _p("string",  "Repo owner"),
        "repo":         _p("string",  "Repository name"),
        "issue_number": _p("integer", "Issue number"),
        "title":        _p("string",  "New title"),
        "body":         _p("string",  "New body"),
        "state":        _p("string",  "open or closed"),
        "labels":       _p("array",   "New labels"),
        "assignees":    _p("array",   "New assignees")},
       ["owner", "repo", "issue_number"]),

    _t("create_issue_comment",
       "Add a comment to an issue or pull request.",
       {"owner":        _p("string",  "Repo owner"),
        "repo":         _p("string",  "Repository name"),
        "issue_number": _p("integer", "Issue/PR number"),
        "body":         _p("string",  "Comment text")},
       ["owner", "repo", "issue_number", "body"]),

    _t("list_issue_comments",
       "List all comments on an issue or pull request.",
       {"owner":        _p("string",  "Repo owner"),
        "repo":         _p("string",  "Repository name"),
        "issue_number": _p("integer", "Issue/PR number")},
       ["owner", "repo", "issue_number"]),

    # ── LABELS & MILESTONES ───────────────────────────────────────
    _t("list_labels",
       "List labels for a repository.",
       {"owner": _p("string", "Repo owner"),
        "repo":  _p("string", "Repository name")},
       ["owner", "repo"]),

    _t("create_label",
       "Create a label in a repository.",
       {"owner":       _p("string", "Repo owner"),
        "repo":        _p("string", "Repository name"),
        "name":        _p("string", "Label name"),
        "color":       _p("string", "Hex color without # (e.g. f29513)"),
        "description": _p("string", "Label description (optional)")},
       ["owner", "repo", "name", "color"]),

    _t("delete_label",
       "Delete a label from a repository.",
       {"owner": _p("string", "Repo owner"),
        "repo":  _p("string", "Repository name"),
        "name":  _p("string", "Label name")},
       ["owner", "repo", "name"]),

    _t("list_milestones",
       "List milestones for a repository.",
       {"owner": _p("string", "Repo owner"),
        "repo":  _p("string", "Repository name"),
        "state": _p("string", "open or closed (default: open)")},
       ["owner", "repo"]),

    _t("create_milestone",
       "Create a milestone.",
       {"owner":       _p("string", "Repo owner"),
        "repo":        _p("string", "Repository name"),
        "title":       _p("string", "Milestone title"),
        "description": _p("string", "Description (optional)"),
        "due_on":      _p("string", "Due date ISO 8601 (optional)")},
       ["owner", "repo", "title"]),

    # ── RELEASES ──────────────────────────────────────────────────
    _t("list_releases",
       "List releases for a repository.",
       {"owner":    _p("string",  "Repo owner"),
        "repo":     _p("string",  "Repository name"),
        "per_page": _p("integer", "Results per page")},
       ["owner", "repo"]),

    _t("get_latest_release",
       "Get the latest published release for a repository.",
       {"owner": _p("string", "Repo owner"),
        "repo":  _p("string", "Repository name")},
       ["owner", "repo"]),

    _t("create_release",
       "Create a new release.",
       {"owner":            _p("string",  "Repo owner"),
        "repo":             _p("string",  "Repository name"),
        "tag_name":         _p("string",  "Tag name (e.g. v1.0.0)"),
        "name":             _p("string",  "Release title"),
        "body":             _p("string",  "Release notes"),
        "draft":            _p("boolean", "Create as draft (default false)"),
        "prerelease":       _p("boolean", "Mark as prerelease (default false)"),
        "target_commitish": _p("string",  "Branch or SHA to tag from")},
       ["owner", "repo", "tag_name"]),

    _t("delete_release",
       "Delete a release.",
       {"owner":      _p("string",  "Repo owner"),
        "repo":       _p("string",  "Repository name"),
        "release_id": _p("integer", "Release ID")},
       ["owner", "repo", "release_id"]),

    # ── ACTIONS / WORKFLOWS ───────────────────────────────────────
    _t("list_workflows",
       "List all workflows in a repository.",
       {"owner": _p("string", "Repo owner"),
        "repo":  _p("string", "Repository name")},
       ["owner", "repo"]),

    _t("get_workflow",
       "Get a specific workflow by ID or filename.",
       {"owner":       _p("string", "Repo owner"),
        "repo":        _p("string", "Repository name"),
        "workflow_id": _p("string", "Workflow ID or filename (e.g. ci.yml)")},
       ["owner", "repo", "workflow_id"]),

    _t("list_workflow_runs",
       "List runs for a specific workflow.",
       {"owner":       _p("string",  "Repo owner"),
        "repo":        _p("string",  "Repository name"),
        "workflow_id": _p("string",  "Workflow ID or filename"),
        "status":      _p("string",  "Filter: completed, in_progress, queued, failure, success"),
        "branch":      _p("string",  "Filter by branch"),
        "per_page":    _p("integer", "Results per page")},
       ["owner", "repo", "workflow_id"]),

    _t("trigger_workflow",
       "Manually trigger a workflow dispatch event.",
       {"owner":       _p("string", "Repo owner"),
        "repo":        _p("string", "Repository name"),
        "workflow_id": _p("string", "Workflow ID or filename"),
        "ref":         _p("string", "Branch or tag to run on"),
        "inputs":      _p("object", "Workflow input key-value pairs (optional)")},
       ["owner", "repo", "workflow_id", "ref"]),

    _t("cancel_workflow_run",
       "Cancel a running workflow run.",
       {"owner":  _p("string",  "Repo owner"),
        "repo":   _p("string",  "Repository name"),
        "run_id": _p("integer", "Workflow run ID")},
       ["owner", "repo", "run_id"]),

    _t("rerun_workflow",
       "Re-run a failed or cancelled workflow run.",
       {"owner":  _p("string",  "Repo owner"),
        "repo":   _p("string",  "Repository name"),
        "run_id": _p("integer", "Workflow run ID")},
       ["owner", "repo", "run_id"]),

    _t("list_workflow_run_jobs",
       "List jobs in a specific workflow run.",
       {"owner":  _p("string",  "Repo owner"),
        "repo":   _p("string",  "Repository name"),
        "run_id": _p("integer", "Workflow run ID")},
       ["owner", "repo", "run_id"]),

    _t("get_workflow_run_logs_url",
       "Get the download URL for workflow run logs.",
       {"owner":  _p("string",  "Repo owner"),
        "repo":   _p("string",  "Repository name"),
        "run_id": _p("integer", "Workflow run ID")},
       ["owner", "repo", "run_id"]),

    # ── SECRETS ───────────────────────────────────────────────────
    _t("list_repo_secrets",
       "List Actions secrets for a repository (names only, not values).",
       {"owner": _p("string", "Repo owner"),
        "repo":  _p("string", "Repository name")},
       ["owner", "repo"]),

    _t("delete_repo_secret",
       "Delete an Actions secret from a repository.",
       {"owner":       _p("string", "Repo owner"),
        "repo":        _p("string", "Repository name"),
        "secret_name": _p("string", "Secret name")},
       ["owner", "repo", "secret_name"]),

    # ── COLLABORATORS & TEAMS ─────────────────────────────────────
    _t("list_collaborators",
       "List collaborators for a repository.",
       {"owner":       _p("string", "Repo owner"),
        "repo":        _p("string", "Repository name"),
        "affiliation": _p("string", "Filter: outside, direct, all (default: all)")},
       ["owner", "repo"]),

    _t("add_collaborator",
       "Add a collaborator to a repository.",
       {"owner":      _p("string", "Repo owner"),
        "repo":       _p("string", "Repository name"),
        "username":   _p("string", "GitHub username to add"),
        "permission": _p("string", "Permission: pull, push, admin, maintain, triage")},
       ["owner", "repo", "username"]),

    _t("remove_collaborator",
       "Remove a collaborator from a repository.",
       {"owner":    _p("string", "Repo owner"),
        "repo":     _p("string", "Repository name"),
        "username": _p("string", "GitHub username to remove")},
       ["owner", "repo", "username"]),

    # ── ORGANIZATIONS ─────────────────────────────────────────────
    _t("list_org_repos",
       "List repositories in an organization.",
       {"org":      _p("string",  "Organization name"),
        "type":     _p("string",  "Type: all, public, private, forks, sources, member"),
        "per_page": _p("integer", "Results per page")},
       ["org"]),

    _t("list_org_members",
       "List members of an organization.",
       {"org":      _p("string",  "Organization name"),
        "role":     _p("string",  "Filter by role: all, admin, member (default: all)"),
        "per_page": _p("integer", "Results per page")},
       ["org"]),

    _t("list_org_teams",
       "List teams in an organization.",
       {"org": _p("string", "Organization name")}, ["org"]),

    # ── SEARCH ────────────────────────────────────────────────────
    _t("search_repos",
       "Search GitHub repositories by keyword and filters.",
       {"query":    _p("string",  "Search query (supports qualifiers like language:python)"),
        "sort":     _p("string",  "Sort by: stars, forks, help-wanted-issues, updated"),
        "order":    _p("string",  "asc or desc (default: desc)"),
        "per_page": _p("integer", "Results per page (max 100)")},
       ["query"]),

    _t("search_code",
       "Search code across GitHub repositories.",
       {"query":    _p("string",  "Search query (supports repo:, path:, language: qualifiers)"),
        "per_page": _p("integer", "Results per page")},
       ["query"]),

    _t("search_issues",
       "Search issues and pull requests across GitHub.",
       {"query":    _p("string",  "Search query (supports is:pr, is:issue, repo:, etc.)"),
        "sort":     _p("string",  "Sort by: comments, reactions, created, updated"),
        "per_page": _p("integer", "Results per page")},
       ["query"]),

    # ── TAGS & WEBHOOKS ───────────────────────────────────────────
    _t("list_tags",
       "List tags for a repository.",
       {"owner":    _p("string",  "Repo owner"),
        "repo":     _p("string",  "Repository name"),
        "per_page": _p("integer", "Results per page")},
       ["owner", "repo"]),

    _t("list_webhooks",
       "List webhooks for a repository.",
       {"owner": _p("string", "Repo owner"),
        "repo":  _p("string", "Repository name")},
       ["owner", "repo"]),

    _t("create_webhook",
       "Create a webhook for a repository.",
       {"owner":        _p("string", "Repo owner"),
        "repo":         _p("string", "Repository name"),
        "url":          _p("string", "Payload URL"),
        "content_type": _p("string", "json or form (default: json)"),
        "events":       _p("array",  "Events to subscribe to (default: ['push'])"),
        "secret":       _p("string", "Webhook secret (optional)"),
        "active":       _p("boolean","Enable webhook (default true)")},
       ["owner", "repo", "url"]),

    _t("delete_webhook",
       "Delete a repository webhook.",
       {"owner":      _p("string",  "Repo owner"),
        "repo":       _p("string",  "Repository name"),
        "hook_id":    _p("integer", "Webhook ID")},
       ["owner", "repo", "hook_id"]),

    # ── RATE LIMIT & META ─────────────────────────────────────────
    _t("get_rate_limit",
       "Get current GitHub API rate limit status for the authenticated user."),
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


async def _dispatch(name: str, a: dict, c: GitHubClient) -> CTR:

    # ── USER / AUTH ───────────────────────────────────────────────
    if name == "get_authenticated_user":
        return ok(c.get("user"))

    if name == "list_my_repos":
        params = []
        if a.get("type"):     params.append(f"type={a['type']}")
        if a.get("sort"):     params.append(f"sort={a['sort']}")
        if a.get("per_page"): params.append(f"per_page={a['per_page']}")
        qs = "?" + "&".join(params) if params else ""
        return ok(c.get(f"user/repos{qs}"))

    if name == "get_user":
        return ok(c.get(f"users/{a['username']}"))

    if name == "list_user_repos":
        pp = a.get("per_page", 30)
        return ok(c.get(f"users/{a['username']}/repos?per_page={pp}"))

    # ── REPOSITORIES ──────────────────────────────────────────────
    if name == "get_repo":
        return ok(c.get(f"repos/{a['owner']}/{a['repo']}"))

    if name == "create_repo":
        payload = {
            "name":        a["name"],
            "description": a.get("description", ""),
            "private":     a.get("private", False),
            "auto_init":   a.get("auto_init", False),
        }
        return ok(c.post("user/repos", payload))

    if name == "update_repo":
        o, r = a["owner"], a["repo"]
        payload = {k: v for k, v in {
            "description":   a.get("description"),
            "private":       a.get("private"),
            "has_issues":    a.get("has_issues"),
            "has_wiki":      a.get("has_wiki"),
            "default_branch":a.get("default_branch"),
        }.items() if v is not None}
        return ok(c.patch(f"repos/{o}/{r}", payload))

    if name == "delete_repo":
        c.delete(f"repos/{a['owner']}/{a['repo']}")
        return ok(f"Repository '{a['owner']}/{a['repo']}' deleted.")

    if name == "fork_repo":
        payload = {}
        if a.get("organization"):
            payload["organization"] = a["organization"]
        return ok(c.post(f"repos/{a['owner']}/{a['repo']}/forks", payload))

    if name == "list_repo_topics":
        return ok(c.get(f"repos/{a['owner']}/{a['repo']}/topics"))

    if name == "replace_repo_topics":
        return ok(c.put(f"repos/{a['owner']}/{a['repo']}/topics", {"names": a["topics"]}))

    # ── BRANCHES & REFS ───────────────────────────────────────────
    if name == "list_branches":
        qs = "?protected=true" if a.get("protected") else ""
        return ok(c.get(f"repos/{a['owner']}/{a['repo']}/branches{qs}"))

    if name == "get_branch":
        return ok(c.get(f"repos/{a['owner']}/{a['repo']}/branches/{quote(a['branch'])}"))

    if name == "create_branch":
        o, r = a["owner"], a["repo"]
        # Resolve the from_ref to a SHA
        ref_data = c.get(f"repos/{o}/{r}/git/ref/heads/{quote(a['from_ref'])}")
        sha = ref_data.get("object", {}).get("sha")
        if not sha:
            # Try as a commit SHA directly
            sha = a["from_ref"]
        c.post(f"repos/{o}/{r}/git/refs", {
            "ref": f"refs/heads/{a['new_branch']}",
            "sha": sha,
        })
        return ok(f"Branch '{a['new_branch']}' created from '{a['from_ref']}'.")

    if name == "delete_branch":
        o, r, b = a["owner"], a["repo"], a["branch"]
        c.delete(f"repos/{o}/{r}/git/refs/heads/{quote(b)}")
        return ok(f"Branch '{b}' deleted.")

    if name == "merge_branches":
        payload = {"base": a["base"], "head": a["head"]}
        if a.get("commit_message"):
            payload["commit_message"] = a["commit_message"]
        return ok(c.post(f"repos/{a['owner']}/{a['repo']}/merges", payload))

    # ── COMMITS ───────────────────────────────────────────────────
    if name == "list_commits":
        o, r = a["owner"], a["repo"]
        params = []
        for k, v in [("sha", a.get("sha")), ("path", a.get("path")),
                     ("author", a.get("author")), ("per_page", a.get("per_page", 30))]:
            if v: params.append(f"{k}={v}")
        qs = "?" + "&".join(params) if params else ""
        return ok(c.get(f"repos/{o}/{r}/commits{qs}"))

    if name == "get_commit":
        return ok(c.get(f"repos/{a['owner']}/{a['repo']}/commits/{a['ref']}"))

    if name == "compare_commits":
        return ok(c.get(f"repos/{a['owner']}/{a['repo']}/compare/{a['base']}...{a['head']}"))

    # ── CONTENTS / FILES ──────────────────────────────────────────
    if name == "get_file_contents":
        o, r, p = a["owner"], a["repo"], a["path"]
        qs = f"?ref={a['ref']}" if a.get("ref") else ""
        data = c.get(f"repos/{o}/{r}/contents/{p}{qs}")
        if isinstance(data, dict) and data.get("encoding") == "base64":
            content = _b64.b64decode(data["content"].replace("\n", "")).decode("utf-8", errors="replace")
            data["content_decoded"] = content
        return ok(data)

    if name == "create_or_update_file":
        o, r, p = a["owner"], a["repo"], a["path"]
        payload = {
            "message": a["message"],
            "content": _b64.b64encode(a["content"].encode()).decode(),
        }
        if a.get("sha"):    payload["sha"]    = a["sha"]
        if a.get("branch"): payload["branch"] = a["branch"]
        return ok(c.put(f"repos/{o}/{r}/contents/{p}", payload))

    if name == "delete_file":
        o, r, p = a["owner"], a["repo"], a["path"]
        payload = {"message": a["message"], "sha": a["sha"]}
        if a.get("branch"): payload["branch"] = a["branch"]
        from urllib.request import Request, urlopen
        import ssl
        url = f"{c.base_url}/repos/{o}/{r}/contents/{p}"
        headers = {
            "Authorization": f"Bearer {c.pat}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "github-mcp-server/1.0",
        }
        req = Request(url, data=json.dumps(payload).encode(), headers=headers, method="DELETE")
        ctx = c._ctx()
        with urlopen(req, context=ctx, timeout=30) as resp:
            body = resp.read()
            return ok(json.loads(body) if body else {"message": f"File '{p}' deleted."})

    if name == "list_repo_contents":
        o, r = a["owner"], a["repo"]
        p  = a.get("path", "")
        qs = f"?ref={a['ref']}" if a.get("ref") else ""
        return ok(c.get(f"repos/{o}/{r}/contents/{p}{qs}"))

    # ── PULL REQUESTS ─────────────────────────────────────────────
    if name == "list_pull_requests":
        o, r = a["owner"], a["repo"]
        params = [f"state={a.get('state', 'open')}"]
        if a.get("base"):     params.append(f"base={a['base']}")
        if a.get("sort"):     params.append(f"sort={a['sort']}")
        if a.get("per_page"): params.append(f"per_page={a['per_page']}")
        return ok(c.get(f"repos/{o}/{r}/pulls?{'&'.join(params)}"))

    if name == "get_pull_request":
        return ok(c.get(f"repos/{a['owner']}/{a['repo']}/pulls/{a['pr_number']}"))

    if name == "create_pull_request":
        payload = {
            "title": a["title"],
            "head":  a["head"],
            "base":  a["base"],
        }
        if a.get("body"):                   payload["body"]                   = a["body"]
        if a.get("draft") is not None:      payload["draft"]                  = a["draft"]
        if a.get("maintainer_can_modify") is not None:
            payload["maintainer_can_modify"] = a["maintainer_can_modify"]
        return ok(c.post(f"repos/{a['owner']}/{a['repo']}/pulls", payload))

    if name == "update_pull_request":
        payload = {k: v for k, v in {
            "title": a.get("title"), "body": a.get("body"),
            "state": a.get("state"), "base": a.get("base"),
        }.items() if v is not None}
        return ok(c.patch(f"repos/{a['owner']}/{a['repo']}/pulls/{a['pr_number']}", payload))

    if name == "merge_pull_request":
        payload = {"merge_method": a.get("merge_method", "merge")}
        if a.get("commit_title"):   payload["commit_title"]   = a["commit_title"]
        if a.get("commit_message"): payload["commit_message"] = a["commit_message"]
        return ok(c.put(f"repos/{a['owner']}/{a['repo']}/pulls/{a['pr_number']}/merge", payload))

    if name == "list_pr_reviews":
        return ok(c.get(f"repos/{a['owner']}/{a['repo']}/pulls/{a['pr_number']}/reviews"))

    if name == "create_pr_review":
        payload = {"event": a["event"]}
        if a.get("body"): payload["body"] = a["body"]
        return ok(c.post(f"repos/{a['owner']}/{a['repo']}/pulls/{a['pr_number']}/reviews", payload))

    if name == "list_pr_files":
        return ok(c.get(f"repos/{a['owner']}/{a['repo']}/pulls/{a['pr_number']}/files"))

    # ── ISSUES ────────────────────────────────────────────────────
    if name == "list_issues":
        o, r = a["owner"], a["repo"]
        params = [f"state={a.get('state', 'open')}"]
        for k in ("labels", "assignee", "sort", "per_page"):
            if a.get(k): params.append(f"{k}={a[k]}")
        return ok(c.get(f"repos/{o}/{r}/issues?{'&'.join(params)}"))

    if name == "get_issue":
        return ok(c.get(f"repos/{a['owner']}/{a['repo']}/issues/{a['issue_number']}"))

    if name == "create_issue":
        payload = {"title": a["title"]}
        for k in ("body", "labels", "assignees", "milestone"):
            if a.get(k) is not None: payload[k] = a[k]
        return ok(c.post(f"repos/{a['owner']}/{a['repo']}/issues", payload))

    if name == "update_issue":
        payload = {k: v for k, v in {
            "title": a.get("title"), "body": a.get("body"),
            "state": a.get("state"), "labels": a.get("labels"),
            "assignees": a.get("assignees"),
        }.items() if v is not None}
        return ok(c.patch(f"repos/{a['owner']}/{a['repo']}/issues/{a['issue_number']}", payload))

    if name == "create_issue_comment":
        return ok(c.post(f"repos/{a['owner']}/{a['repo']}/issues/{a['issue_number']}/comments",
                         {"body": a["body"]}))

    if name == "list_issue_comments":
        return ok(c.get(f"repos/{a['owner']}/{a['repo']}/issues/{a['issue_number']}/comments"))

    # ── LABELS & MILESTONES ───────────────────────────────────────
    if name == "list_labels":
        return ok(c.get(f"repos/{a['owner']}/{a['repo']}/labels"))

    if name == "create_label":
        payload = {"name": a["name"], "color": a["color"]}
        if a.get("description"): payload["description"] = a["description"]
        return ok(c.post(f"repos/{a['owner']}/{a['repo']}/labels", payload))

    if name == "delete_label":
        c.delete(f"repos/{a['owner']}/{a['repo']}/labels/{quote(a['name'])}")
        return ok(f"Label '{a['name']}' deleted.")

    if name == "list_milestones":
        state = a.get("state", "open")
        return ok(c.get(f"repos/{a['owner']}/{a['repo']}/milestones?state={state}"))

    if name == "create_milestone":
        payload = {"title": a["title"]}
        if a.get("description"): payload["description"] = a["description"]
        if a.get("due_on"):      payload["due_on"]      = a["due_on"]
        return ok(c.post(f"repos/{a['owner']}/{a['repo']}/milestones", payload))

    # ── RELEASES ──────────────────────────────────────────────────
    if name == "list_releases":
        pp = a.get("per_page", 30)
        return ok(c.get(f"repos/{a['owner']}/{a['repo']}/releases?per_page={pp}"))

    if name == "get_latest_release":
        return ok(c.get(f"repos/{a['owner']}/{a['repo']}/releases/latest"))

    if name == "create_release":
        payload = {"tag_name": a["tag_name"]}
        for k in ("name", "body", "draft", "prerelease", "target_commitish"):
            if a.get(k) is not None: payload[k] = a[k]
        return ok(c.post(f"repos/{a['owner']}/{a['repo']}/releases", payload))

    if name == "delete_release":
        c.delete(f"repos/{a['owner']}/{a['repo']}/releases/{a['release_id']}")
        return ok(f"Release {a['release_id']} deleted.")

    # ── ACTIONS / WORKFLOWS ───────────────────────────────────────
    if name == "list_workflows":
        return ok(c.get(f"repos/{a['owner']}/{a['repo']}/actions/workflows"))

    if name == "get_workflow":
        return ok(c.get(f"repos/{a['owner']}/{a['repo']}/actions/workflows/{a['workflow_id']}"))

    if name == "list_workflow_runs":
        o, r, wid = a["owner"], a["repo"], a["workflow_id"]
        params = []
        for k in ("status", "branch", "per_page"):
            if a.get(k): params.append(f"{k}={a[k]}")
        qs = "?" + "&".join(params) if params else ""
        return ok(c.get(f"repos/{o}/{r}/actions/workflows/{wid}/runs{qs}"))

    if name == "trigger_workflow":
        payload = {"ref": a["ref"]}
        if a.get("inputs"): payload["inputs"] = a["inputs"]
        c.post(f"repos/{a['owner']}/{a['repo']}/actions/workflows/{a['workflow_id']}/dispatches", payload)
        return ok(f"Workflow '{a['workflow_id']}' triggered on '{a['ref']}'.")

    if name == "cancel_workflow_run":
        c.post(f"repos/{a['owner']}/{a['repo']}/actions/runs/{a['run_id']}/cancel")
        return ok(f"Workflow run {a['run_id']} cancelled.")

    if name == "rerun_workflow":
        c.post(f"repos/{a['owner']}/{a['repo']}/actions/runs/{a['run_id']}/rerun")
        return ok(f"Workflow run {a['run_id']} re-run triggered.")

    if name == "list_workflow_run_jobs":
        return ok(c.get(f"repos/{a['owner']}/{a['repo']}/actions/runs/{a['run_id']}/jobs"))

    if name == "get_workflow_run_logs_url":
        # Returns a redirect — capture the Location header
        o, r, run_id = a["owner"], a["repo"], a["run_id"]
        url = f"{c.base_url}/repos/{o}/{r}/actions/runs/{run_id}/logs"
        try:
            c.get(f"repos/{o}/{r}/actions/runs/{run_id}/logs")
        except Exception:
            pass
        return ok(f"Logs URL: {url}  (GET this URL with your PAT to download)")

    # ── SECRETS ───────────────────────────────────────────────────
    if name == "list_repo_secrets":
        return ok(c.get(f"repos/{a['owner']}/{a['repo']}/actions/secrets"))

    if name == "delete_repo_secret":
        c.delete(f"repos/{a['owner']}/{a['repo']}/actions/secrets/{a['secret_name']}")
        return ok(f"Secret '{a['secret_name']}' deleted.")

    # ── COLLABORATORS & TEAMS ─────────────────────────────────────
    if name == "list_collaborators":
        aff = a.get("affiliation", "all")
        return ok(c.get(f"repos/{a['owner']}/{a['repo']}/collaborators?affiliation={aff}"))

    if name == "add_collaborator":
        payload = {}
        if a.get("permission"): payload["permission"] = a["permission"]
        c.put(f"repos/{a['owner']}/{a['repo']}/collaborators/{a['username']}", payload)
        return ok(f"Collaborator '{a['username']}' added to '{a['owner']}/{a['repo']}'.")

    if name == "remove_collaborator":
        c.delete(f"repos/{a['owner']}/{a['repo']}/collaborators/{a['username']}")
        return ok(f"Collaborator '{a['username']}' removed.")

    # ── ORGANIZATIONS ─────────────────────────────────────────────
    if name == "list_org_repos":
        params = [f"type={a.get('type', 'all')}"]
        if a.get("per_page"): params.append(f"per_page={a['per_page']}")
        return ok(c.get(f"orgs/{a['org']}/repos?{'&'.join(params)}"))

    if name == "list_org_members":
        role = a.get("role", "all")
        pp   = a.get("per_page", 30)
        return ok(c.get(f"orgs/{a['org']}/members?role={role}&per_page={pp}"))

    if name == "list_org_teams":
        return ok(c.get(f"orgs/{a['org']}/teams"))

    # ── SEARCH ────────────────────────────────────────────────────
    if name == "search_repos":
        params = [f"q={quote(a['query'])}"]
        if a.get("sort"):     params.append(f"sort={a['sort']}")
        if a.get("order"):    params.append(f"order={a['order']}")
        if a.get("per_page"): params.append(f"per_page={a['per_page']}")
        return ok(c.get(f"search/repositories?{'&'.join(params)}"))

    if name == "search_code":
        params = [f"q={quote(a['query'])}"]
        if a.get("per_page"): params.append(f"per_page={a['per_page']}")
        return ok(c.get(f"search/code?{'&'.join(params)}"))

    if name == "search_issues":
        params = [f"q={quote(a['query'])}"]
        if a.get("sort"):     params.append(f"sort={a['sort']}")
        if a.get("per_page"): params.append(f"per_page={a['per_page']}")
        return ok(c.get(f"search/issues?{'&'.join(params)}"))

    # ── TAGS & WEBHOOKS ───────────────────────────────────────────
    if name == "list_tags":
        pp = a.get("per_page", 30)
        return ok(c.get(f"repos/{a['owner']}/{a['repo']}/tags?per_page={pp}"))

    if name == "list_webhooks":
        return ok(c.get(f"repos/{a['owner']}/{a['repo']}/hooks"))

    if name == "create_webhook":
        payload = {
            "name":   "web",
            "active": a.get("active", True),
            "events": a.get("events", ["push"]),
            "config": {
                "url":          a["url"],
                "content_type": a.get("content_type", "json"),
            },
        }
        if a.get("secret"): payload["config"]["secret"] = a["secret"]
        return ok(c.post(f"repos/{a['owner']}/{a['repo']}/hooks", payload))

    if name == "delete_webhook":
        c.delete(f"repos/{a['owner']}/{a['repo']}/hooks/{a['hook_id']}")
        return ok(f"Webhook {a['hook_id']} deleted.")

    # ── RATE LIMIT ────────────────────────────────────────────────
    if name == "get_rate_limit":
        return ok(c.get("rate_limit"))

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
        "status": "ok", "server": "github-mcp",
        "tools": len(ALL_TOOLS), "url": CONFIG["GITHUB_URL"],
    })

async def handle_sse(request):
    sid = str(uuid.uuid4())
    q: asyncio.Queue = asyncio.Queue()
    _sessions[sid] = q
    resp = web.StreamResponse()
    resp.headers.update({
        "Content-Type": "text/event-stream", "Cache-Control": "no-cache",
        "Connection": "keep-alive", "Access-Control-Allow-Origin": "*",
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
        _sessions.pop(sid, None)
    return resp

async def handle_message(request):
    sid = request.rel_url.query.get("session_id")
    q   = _sessions.get(sid)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    req_id = body.get("id"); method = body.get("method", ""); params = body.get("params", {})
    resp = None
    if method == "initialize":
        resp = _rpc_ok(req_id, {"protocolVersion": "2025-03-26",
                                "capabilities": {"tools": {}},
                                "serverInfo": {"name": "github-mcp", "version": "1.0.0"}})
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
        if q: await q.put(resp)
        else: return web.json_response(resp)
    return web.Response(status=202)

async def _process_jsonrpc(body):
    req_id = body.get("id"); method = body.get("method", ""); params = body.get("params", {})
    if method == "initialize":
        return _rpc_ok(req_id, {"protocolVersion": "2025-03-26",
                                "capabilities": {"tools": {}},
                                "serverInfo": {"name": "github-mcp", "version": "1.0.0"}})
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
    print(f"║       GitHub MCP Server — Running            ║")
    print(f"╠══════════════════════════════════════════════╣")
    print(f"║  POST  http://{host}:{port}/mcp")
    print(f"║  GET   http://{host}:{port}/mcp  (SSE)")
    print(f"║  GET   http://{host}:{port}/sse  (legacy)")
    print(f"║  Tools : {len(ALL_TOOLS)}")
    print(f"║  URL   : {CONFIG['GITHUB_URL']}")
    print(f"╚══════════════════════════════════════════════╝")
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
