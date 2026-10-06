#!/usr/bin/env python3
"""
AI Project Factory -- GitHub Actions Runner
===========================================
All secrets/config come from environment variables set by GitHub Actions.

Flow:
  1.  Validate required environment variables.
  2.  Pick AI category (day-of-year rotation or OVERRIDE_CATEGORY).
  3.  Search GitHub + HuggingFace for reference context (failures non-fatal).
  4.  Call Groq to design an ORIGINAL project plan (JSON).
  5.  Collision-check the target repo name on GitHub.
  6.  [DRY_RUN stops here — prints plan, makes no mutations]
  7.  Create Supabase project record (status=generating).
  8.  Create GitHub repository.
  9.  Generate each project file with Groq (retries + backoff).
  10. Validate & sanitise file paths (path-traversal guard).
  11. Upload files to the new GitHub repo via Contents API.
  12. Upload generated CI workflow to the new repo.
  13. Update Supabase record (status=published).

Exit codes:
  0  success (or dry-run complete)
  1  configuration/environment error
  2  Groq planner failure
  3  GitHub repo-creation failure
  4  partial file-upload failure
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("factory_run.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("factory")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_USER_AGENT = "AI-Project-Factory/1.0 (github.com/rafiaabbasi24/ai-project-factory)"
GITHUB_API_URL = "https://api.github.com"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GITHUB_DEFAULT_BRANCH = "main"

PERMISSIVE_LICENSES = {
    "MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause",
    "ISC", "CC0-1.0", "Unlicense", "MPL-2.0",
}

CATEGORY_MAP: dict[str, dict[str, str]] = {
    "LLM": {
        "github": "large language model LLM OR RAG OR agent",
        "hf": "language model",
    },
    "Generative AI": {
        "github": "generative AI OR diffusion OR multimodal",
        "hf": "generative",
    },
    "Machine Learning": {
        "github": "machine learning classification regression anomaly detection",
        "hf": "tabular classification",
    },
    "Deep Learning": {
        "github": "deep learning CNN computer vision neural network",
        "hf": "image classification",
    },
    "NLP": {
        "github": "NLP natural language processing sentiment NER",
        "hf": "text classification",
    },
    "Transformers": {
        "github": "transformers BERT ViT encoder decoder attention",
        "hf": "transformers",
    },
    "Reinforcement Learning": {
        "github": "reinforcement learning DQN PPO Q-learning gymnasium",
        "hf": "reinforcement learning",
    },
}

# ---------------------------------------------------------------------------
# Environment loading
# ---------------------------------------------------------------------------

def _require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        log.error("Required environment variable %s is not set or is empty.", name)
        sys.exit(1)
    return val


def _optional(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# Groq
GROQ_API_KEY: str = _require("GROQ_API_KEY")
GROQ_MODEL: str = _optional("GROQ_MODEL", "openai/gpt-oss-120b")

# GitHub owner: injected from github.repository_owner in the workflow;
# GITHUB_REPOSITORY_OWNER is also set automatically by Actions.
GITHUB_OWNER: str = (
    _optional("GITHUB_OWNER") or _optional("GITHUB_REPOSITORY_OWNER")
).strip()
if not GITHUB_OWNER:
    log.error(
        "GITHUB_OWNER is empty. Ensure the workflow sets: "
        "GITHUB_OWNER: ${{ github.repository_owner }}"
    )
    sys.exit(1)

# GitHub token: prefer PAT (for cross-user repo creation), fall back to built-in token
FACTORY_GITHUB_TOKEN: str = (
    _optional("FACTORY_GITHUB_TOKEN") or _optional("GITHUB_TOKEN")
).strip()
if not FACTORY_GITHUB_TOKEN:
    log.error("No GitHub token found (FACTORY_GITHUB_TOKEN or GITHUB_TOKEN).")
    sys.exit(1)

PROJECT_VISIBILITY: str = _optional("PROJECT_VISIBILITY", "public")

# HuggingFace (optional)
HF_TOKEN: str = _optional("HF_TOKEN", "")

# Supabase (optional — graceful degradation if missing)
SUPABASE_URL: str = _optional("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY: str = (
    _optional("SUPABASE_SERVICE_ROLE_KEY") or _optional("SUPABASE_SECRET_KEY")
)

CATEGORIES: list[str] = [
    c.strip()
    for c in _optional(
        "PROJECT_CATEGORIES",
        "LLM,Generative AI,Machine Learning,Deep Learning,NLP,Transformers,Reinforcement Learning",
    ).split(",")
    if c.strip()
]
PROJECT_GITHUB_PREFIX: str = _optional("PROJECT_GITHUB_PREFIX", "ai-daily-")
MAX_FILES: int = int(_optional("MAX_FILES", "10"))
OVERRIDE_CATEGORY: str = _optional("OVERRIDE_CATEGORY", "")
DRY_RUN: bool = _optional("DRY_RUN", "false").lower() == "true"
RUN_DATE: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

_UNSAFE = re.compile(
    r"(^|[/\\])\.\.([/\\]|$)"  # traversal
    r"|^[/\\]"                  # absolute UNIX
    r"|^[A-Za-z]:[/\\]"        # absolute Windows
    r"|[<>:\"|?*\x00-\x1f]",   # illegal chars
)


def safe_path(path: str) -> str | None:
    """Return normalised path if safe for use inside a repo, else None."""
    p = path.strip().replace("\\", "/")
    if not p or _UNSAFE.search(p):
        return None
    return re.sub(r"/+", "/", p)


# ---------------------------------------------------------------------------
# HTTP core — retries, backoff, safe error logging
# ---------------------------------------------------------------------------

class APIError(Exception):
    def __init__(self, subsystem: str, status: int, body: str) -> None:
        self.subsystem = subsystem
        self.status = status
        self.body = body
        super().__init__(f"[{subsystem}] HTTP {status}")


def _http(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    payload: Any = None,
    timeout: int = 30,
    subsystem: str = "HTTP",
    retries: int = 3,
) -> Any:
    """Core HTTP helper. Retries 5xx/429/408, raises APIError on 4xx."""
    h: dict[str, str] = {"User-Agent": _USER_AGENT, **(headers or {})}
    data: bytes | None = None
    if payload is not None:
        data = json.dumps(payload).encode()
        h["Content-Type"] = "application/json"

    last_exc: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                body = exc.read().decode("utf-8", errors="replace")
                # Redact anything that looks like a credential
                safe_body = re.sub(
                    r"(bearer|token|key|secret|password)\s*[=:]\s*\S+",
                    r"\1=<redacted>",
                    body[:400],
                    flags=re.IGNORECASE,
                )
            except Exception:
                safe_body = "(unreadable)"

            if status in (401, 403, 404, 422) or (400 <= status < 500 and status not in (408, 429)):
                log.error(
                    "[%s] %s %s -> HTTP %d (non-retriable): %s",
                    subsystem, method, url, status, safe_body,
                )
                raise APIError(subsystem, status, safe_body)

            log.warning(
                "[%s] %s %s -> HTTP %d (attempt %d/%d): %s",
                subsystem, method, url, status, attempt + 1, retries, safe_body,
            )
            last_exc = exc
        except (urllib.error.URLError, OSError) as exc:
            log.warning(
                "[%s] %s %s -> network error (attempt %d/%d): %s",
                subsystem, method, url, attempt + 1, retries, exc,
            )
            last_exc = exc

        if attempt < retries - 1:
            wait = 2 ** attempt
            log.info("[%s] Retrying in %ds...", subsystem, wait)
            time.sleep(wait)

    log.error("[%s] %s %s failed after %d attempts: %s", subsystem, method, url, retries, last_exc)
    return None


def _get(url: str, *, headers: dict | None = None, timeout: int = 15, subsystem: str = "HTTP") -> Any:
    try:
        return _http("GET", url, headers=headers, timeout=timeout, subsystem=subsystem)
    except APIError:
        return None


def _post(url: str, payload: Any, *, headers: dict | None = None, timeout: int = 120, subsystem: str = "HTTP") -> Any:
    try:
        return _http("POST", url, headers=headers, payload=payload, timeout=timeout, subsystem=subsystem)
    except APIError:
        return None


def _put(url: str, payload: Any, *, headers: dict | None = None, timeout: int = 60, subsystem: str = "HTTP") -> Any:
    try:
        return _http("PUT", url, headers=headers, payload=payload, timeout=timeout, subsystem=subsystem)
    except APIError:
        return None


def _patch(url: str, payload: Any, *, headers: dict | None = None, timeout: int = 30, subsystem: str = "HTTP") -> Any:
    try:
        return _http("PATCH", url, headers=headers, payload=payload, timeout=timeout, subsystem=subsystem)
    except APIError:
        return None


# ---------------------------------------------------------------------------
# Groq
# ---------------------------------------------------------------------------

def groq_chat(
    messages: list[dict],
    *,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    json_mode: bool = False,
    retries: int = 3,
) -> str | None:
    """
    Call Groq chat-completions.
    Key fix: explicit User-Agent prevents WAF 403 (error code 1010).
    """
    payload: dict[str, Any] = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    headers = {
        "User-Agent": _USER_AGENT,   # CRITICAL — prevents 403/1010 from Groq WAF
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    for attempt in range(retries):
        try:
            result = _http(
                "POST", GROQ_API_URL,
                headers=headers, payload=payload,
                timeout=120, subsystem="Groq", retries=1,
            )
            if isinstance(result, dict) and "choices" in result:
                return result["choices"][0]["message"]["content"]
            log.error("[Groq] Unexpected response (attempt %d/%d): %s", attempt + 1, retries, str(result)[:200])
        except APIError as exc:
            log.error(
                "[Groq] HTTP %d error. Check GROQ_API_KEY validity and GROQ_MODEL='%s'.",
                exc.status, GROQ_MODEL,
            )
            if exc.status == 403:
                log.error("[Groq] 403 usually means bad API key or WAF block. Aborting retries.")
                break
        except Exception as exc:
            log.warning("[Groq] Transient error (attempt %d/%d): %s", attempt + 1, retries, exc)

        if attempt < retries - 1:
            wait = 2 ** attempt
            log.info("[Groq] Retrying in %ds...", wait)
            time.sleep(wait)

    return None


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

def _gh_h() -> dict[str, str]:
    return {
        "User-Agent": _USER_AGENT,
        "Authorization": f"token {FACTORY_GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def github_repo_exists(name: str) -> bool:
    r = _get(f"{GITHUB_API_URL}/repos/{GITHUB_OWNER}/{name}", headers=_gh_h(), subsystem="GitHub")
    return isinstance(r, dict) and "id" in r


def github_create_repo(name: str, description: str) -> str | None:
    r = _post(
        f"{GITHUB_API_URL}/user/repos",
        {"name": name, "description": description[:300], "private": PROJECT_VISIBILITY != "public", "auto_init": False},
        headers=_gh_h(), subsystem="GitHub",
    )
    if isinstance(r, dict) and "html_url" in r:
        return r["html_url"]
    return None


def github_upload_file(repo: str, path: str, content: str, msg: str) -> bool:
    sp = safe_path(path)
    if not sp:
        log.error("[GitHub] Unsafe path rejected: %r", path)
        return False
    encoded = base64.b64encode(content.encode("utf-8")).decode()
    url = f"{GITHUB_API_URL}/repos/{GITHUB_OWNER}/{repo}/contents/{sp}"
    existing = _get(url, headers=_gh_h(), subsystem="GitHub")
    body: dict[str, Any] = {"message": msg, "content": encoded, "branch": GITHUB_DEFAULT_BRANCH}
    if isinstance(existing, dict) and "sha" in existing:
        body["sha"] = existing["sha"]
    r = _put(url, body, headers=_gh_h(), subsystem="GitHub")
    return isinstance(r, dict)


def search_github_repos(query: str, limit: int = 5) -> list[dict]:
    url = f"{GITHUB_API_URL}/search/repositories?q={urllib.parse.quote(query)}&sort=stars&order=desc&per_page={limit}"
    try:
        r = _get(url, headers=_gh_h(), timeout=15, subsystem="GitHub")
    except Exception:
        return []
    if not isinstance(r, dict) or "items" not in r:
        return []
    out = []
    for item in r["items"][:limit]:
        lic = "unknown"
        if isinstance(item.get("license"), dict):
            lic = item["license"].get("spdx_id") or "unknown"
        out.append({
            "type": "github_repo", "name": item.get("full_name", ""),
            "url": item.get("html_url", ""), "license": lic,
            "description": (item.get("description") or "")[:200],
        })
    return out


# ---------------------------------------------------------------------------
# HuggingFace
# ---------------------------------------------------------------------------

def _hf_h() -> dict[str, str]:
    h: dict[str, str] = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    if HF_TOKEN:
        h["Authorization"] = f"Bearer {HF_TOKEN}"
    return h


def search_hf_models(query: str, limit: int = 5) -> list[dict]:
    url = (
        f"https://huggingface.co/api/models"
        f"?search={urllib.parse.quote(query)}&limit={limit}&sort=downloads&direction=-1"
    )
    try:
        r = _get(url, headers=_hf_h(), timeout=15, subsystem="HuggingFace")
    except Exception as exc:
        log.warning("[HuggingFace] Search failed (non-fatal): %s", exc)
        return []
    if not isinstance(r, list):
        log.warning("[HuggingFace] Unexpected response type: %s", type(r).__name__)
        return []
    out = []
    for m in r:
        lic = "unknown"
        if isinstance(m.get("cardData"), dict):
            lic = m["cardData"].get("license") or "unknown"
        out.append({
            "type": "hf_model", "name": m.get("modelId", ""),
            "url": f"https://huggingface.co/{m.get('modelId', '')}",
            "license": lic,
        })
    return out


def filter_permissive(sources: list[dict]) -> list[dict]:
    return [s for s in sources if s.get("license") in PERMISSIVE_LICENSES or s.get("license") == "unknown"]


# ---------------------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------------------

def _sb_ok() -> bool:
    return bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)


def _sb_h() -> dict[str, str]:
    return {
        "User-Agent": _USER_AGENT,
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def supabase_insert(table: str, row: dict) -> dict | list | None:
    if not _sb_ok():
        log.warning("[Supabase] Not configured — skipping insert into %s.", table)
        return {"id": "local-only"}
    result = _post(f"{SUPABASE_URL}/rest/v1/{table}", row, headers=_sb_h(), subsystem="Supabase")
    if result is None:
        log.error("[Supabase] Insert into %s FAILED.", table)
    return result


def supabase_update(table: str, row_id: str, updates: dict) -> bool:
    if not _sb_ok():
        log.warning("[Supabase] Not configured — skipping update on %s id=%s.", table, row_id)
        return False
    result = _patch(f"{SUPABASE_URL}/rest/v1/{table}?id=eq.{row_id}", updates, headers=_sb_h(), subsystem="Supabase")
    if result is None:
        log.error("[Supabase] Update on %s id=%s FAILED.", table, row_id)
        return False
    return True


def supabase_select(table: str, filters: str) -> list:
    if not _sb_ok():
        return []
    r = _get(f"{SUPABASE_URL}/rest/v1/{table}?{filters}&limit=1", headers=_sb_h(), subsystem="Supabase")
    return r if isinstance(r, list) else []


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_PLANNER_SYSTEM = (
    "You are a senior AI/ML engineer designing ORIGINAL portfolio projects. "
    "Projects must be unique implementations — never clones of existing repos. "
    "Use reference sources only as inspiration and always attribute with URLs."
)


def _planner_user(category: str, source_ctx: str) -> str:
    return (
        f"Design an ORIGINAL {category} project for a developer portfolio.\n\n"
        f"Reference sources (inspiration only — do NOT copy code):\n"
        f"{source_ctx or '(none available)'}\n\n"
        f"Today: {RUN_DATE}\n\n"
        "Respond with ONLY a valid JSON object — no markdown fences — with EXACTLY these keys:\n"
        '{\n'
        '  "project_name": "Human-readable title (max 60 chars)",\n'
        '  "repo_name_suffix": "short-slug-3-to-5-words (no ai-daily- prefix)",\n'
        '  "description": "One-sentence GitHub repo description (max 120 chars)",\n'
        '  "difficulty": "beginner|intermediate|advanced",\n'
        '  "tech_stack": ["Python", "..."],\n'
        '  "files": [\n'
        '    {"path": "README.md", "description": "Project overview, setup, usage"},\n'
        '    {"path": "requirements.txt", "description": "Python dependencies"},\n'
        '    {"path": "src/main.py", "description": "Main entry point"},\n'
        '    {"path": "tests/test_main.py", "description": "Unit tests"}\n'
        '  ],\n'
        '  "source_attributions": ["https://..."]\n'
        '}\n\n'
        f"Rules: max {MAX_FILES} files; must include README.md, requirements.txt, "
        "at least one src/ file, at least one tests/ file; "
        "all paths relative (no leading slash, no ..); project must be complete and runnable."
    )


def _file_user(fp: str, desc: str, plan: dict, category: str) -> str:
    return (
        f'Write the file "{fp}" for the project "{plan.get("project_name", "AI Project")}".\n\n'
        f"Category: {category}\n"
        f"Tech stack: {', '.join(plan.get('tech_stack', []))}\n"
        f"File purpose: {desc}\n\n"
        "Requirements:\n"
        "- Complete, production-quality code — no TODO stubs or placeholders.\n"
        "- All imports must be real packages installable via pip.\n"
        "- Include docstrings and inline comments.\n"
        "- If this is a test file, write real assertions against actual logic.\n"
        "- Return ONLY the raw file content — no markdown fences, no explanation."
    )


# ---------------------------------------------------------------------------
# Generated CI (injected into every created repo)
# ---------------------------------------------------------------------------

_GENERATED_CI = """\
name: CI
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
jobs:
  build-and-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          if [ -f requirements.txt ]; then pip install -r requirements.txt; fi
      - name: Lint
        run: |
          pip install flake8 --quiet
          flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics
      - name: Run tests
        run: |
          if [ -d tests ]; then
            python -m pytest tests/ -v --tb=short
          else
            echo "No tests/ directory — skipping."
          fi
"""


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def slugify(text: str) -> str:
    t = text.lower()
    t = re.sub(r"[^a-z0-9\s-]", "", t)
    t = re.sub(r"[\s_]+", "-", t)
    t = re.sub(r"-+", "-", t)
    return t.strip("-")[:50]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("=" * 60)
    log.info("AI Project Factory  %s", RUN_DATE)
    log.info("GITHUB_OWNER:  %s", GITHUB_OWNER)
    log.info("GROQ_MODEL:    %s", GROQ_MODEL)
    log.info("DRY_RUN:       %s", DRY_RUN)
    log.info("SUPABASE_URL:  %s", "configured" if SUPABASE_URL else "NOT configured")
    log.info("=" * 60)

    # 1. Category
    if OVERRIDE_CATEGORY and OVERRIDE_CATEGORY in CATEGORY_MAP:
        category = OVERRIDE_CATEGORY
        log.info("Category (override): %s", category)
    else:
        day = datetime.now(timezone.utc).timetuple().tm_yday
        category = CATEGORIES[day % len(CATEGORIES)]
        log.info("Category (auto day=%d): %s", day, category)
    cfg = CATEGORY_MAP.get(category, CATEGORY_MAP["Generative AI"])

    # 2. Reference sources (non-fatal)
    log.info("Searching GitHub: %s", cfg["github"])
    gh_src = search_github_repos(cfg["github"], limit=5)
    log.info("  GitHub results: %d", len(gh_src))

    log.info("Searching HuggingFace: %s", cfg["hf"])
    hf_src = search_hf_models(cfg["hf"], limit=5)
    log.info("  HuggingFace results: %d", len(hf_src))

    sources = filter_permissive(gh_src + hf_src)
    source_ctx = "\n".join(
        f"- [{s['type']}] {s['name']} ({s.get('license','?')}): "
        f"{s.get('description', s.get('url', ''))}"
        for s in sources[:8]
    )

    # 3. Groq planner
    log.info("Calling Groq planner (model=%s)...", GROQ_MODEL)
    plan_raw = groq_chat(
        [
            {"role": "system", "content": _PLANNER_SYSTEM},
            {"role": "user",   "content": _planner_user(category, source_ctx)},
        ],
        temperature=0.85, max_tokens=2048, json_mode=True,
    )
    if not plan_raw:
        log.error("[Groq] Planner returned no output. Aborting.")
        sys.exit(2)
    try:
        plan = json.loads(plan_raw)
    except json.JSONDecodeError as exc:
        log.error("[Groq] Planner JSON parse failed: %s\nRaw (truncated): %s", exc, plan_raw[:400])
        sys.exit(2)

    log.info("Plan: %s", plan.get("project_name"))
    log.info("Files: %s", [f["path"] for f in plan.get("files", [])])

    # 4. Repo name + collision check
    suffix = slugify(plan.get("repo_name_suffix") or plan.get("project_name", "project"))
    repo_name = f"{PROJECT_GITHUB_PREFIX}{suffix}"
    if github_repo_exists(repo_name):
        repo_name = f"{repo_name}-{RUN_DATE}"
    log.info("Repo name: %s", repo_name)

    # 5. Supabase duplicate check
    existing = supabase_select("projects", f"repo_name=eq.{repo_name}&status=eq.published")
    if existing:
        log.warning("[Supabase] '%s' already published. Skipping.", repo_name)
        sys.exit(0)

    # === DRY RUN boundary ===
    if DRY_RUN:
        log.info("DRY RUN complete — plan (no mutations performed):")
        log.info("%s", json.dumps(plan, indent=2))
        sys.exit(0)

    # 6. Supabase record
    run_id = hashlib.md5(f"{repo_name}-{RUN_DATE}".encode()).hexdigest()[:16]
    sb = supabase_insert("projects", {
        "project_name": plan.get("project_name", repo_name),
        "repo_name": repo_name, "category": category,
        "description": plan.get("description", ""),
        "difficulty": plan.get("difficulty", "intermediate"),
        "tech_stack": plan.get("tech_stack", []),
        "source_urls": plan.get("source_attributions", []),
        "plan": plan, "github_owner": GITHUB_OWNER,
        "status": "generating", "generation_run_id": run_id,
    })
    project_id: str | None = None
    if isinstance(sb, list) and sb:
        project_id = sb[0].get("id")
    elif isinstance(sb, dict):
        project_id = sb.get("id")
    log.info("[Supabase] Project ID: %s", project_id)

    # 7. Create GitHub repo
    log.info("[GitHub] Creating: %s/%s", GITHUB_OWNER, repo_name)
    repo_url = github_create_repo(repo_name, (plan.get("description") or "")[:120])
    if not repo_url:
        log.error("[GitHub] Repo creation failed. Aborting.")
        if project_id:
            supabase_update("projects", project_id, {"status": "error", "error_message": "GitHub repo creation failed"})
        sys.exit(3)
    log.info("[GitHub] Repo: %s", repo_url)
    time.sleep(2)

    # 8–11. Generate + upload files
    uploaded: list[str] = []
    failed: list[str] = []
    for fi in plan.get("files", [])[:MAX_FILES]:
        raw_path = fi.get("path", "")
        sp = safe_path(raw_path)
        if not sp:
            log.error("[Security] Rejecting unsafe path: %r", raw_path)
            failed.append(raw_path)
            continue

        log.info("[Groq] Generating: %s", sp)
        content: str | None = None
        for attempt in range(3):
            content = groq_chat(
                [{"role": "user", "content": _file_user(sp, fi.get("description", ""), plan, category)}],
                temperature=0.6, max_tokens=4096,
            )
            if content:
                break
            time.sleep(2 ** attempt)
        if not content:
            log.error("[Groq] Failed to generate %s after 3 attempts.", sp)
            failed.append(sp)
            continue

        # Strip accidental markdown fences
        content = re.sub(r"^```[a-zA-Z0-9]*\r?\n?", "", content)
        content = re.sub(r"\r?\n?```\s*$", "", content)

        if github_upload_file(repo_name, sp, content, f"feat: add {sp}"):
            uploaded.append(sp)
            log.info("[GitHub] Uploaded: %s", sp)
        else:
            log.error("[GitHub] Upload FAILED: %s", sp)
            failed.append(sp)
        time.sleep(1)

    # 12. CI workflow
    if github_upload_file(repo_name, ".github/workflows/ci.yml", _GENERATED_CI, "ci: add GitHub Actions CI"):
        uploaded.append(".github/workflows/ci.yml")
        log.info("[GitHub] CI workflow uploaded.")
    else:
        log.warning("[GitHub] CI workflow upload failed.")
        failed.append(".github/workflows/ci.yml")

    # 13. Supabase final update
    if project_id:
        ok = supabase_update("projects", project_id, {
            "status": "published",
            "github_url": repo_url,
            "github_actions_url": f"{repo_url}/actions",
            "github_default_branch": GITHUB_DEFAULT_BRANCH,
            "published_at": datetime.now(timezone.utc).isoformat(),
        })
        if not ok:
            log.error(
                "[Supabase] IMPORTANT: repo published at %s but status update FAILED. "
                "Manually set id=%s to status=published.", repo_url, project_id,
            )

    # Summary
    log.info("=" * 60)
    if failed:
        log.warning("PARTIAL SUCCESS — uploaded %d/%d, failed: %s",
                    len(uploaded), len(plan.get("files", [])) + 1, failed)
    else:
        log.info("SUCCESS")
    log.info("Project: %s", plan.get("project_name"))
    log.info("Repo:    %s", repo_url)
    log.info("Files:   %d uploaded", len(uploaded))
    log.info("=" * 60)

    if failed:
        sys.exit(4)


if __name__ == "__main__":
    main()
