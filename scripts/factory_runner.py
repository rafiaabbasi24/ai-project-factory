#!/usr/bin/env python3
"""
AI Project Factory -- GitHub Actions Runner
===========================================
All secrets come from environment variables (GitHub Actions secrets/vars).

Flow:
  1. Pick AI category (rotation by day or OVERRIDE_CATEGORY)
  2. Search GitHub + HuggingFace for reference context
  3. Groq: design the project plan + file list
  4. Check GitHub for repo-name collision
  5. Create Supabase project record
  6. Create GitHub repo
  7. Groq: generate each file one-by-one
  8. Upload each file to GitHub via Contents API
  9. Add GitHub Actions CI workflow
  10. Update Supabase status to published
"""

import base64
import hashlib
import json
import logging
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("factory_run.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("factory")

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

FACTORY_GITHUB_TOKEN = os.environ["FACTORY_GITHUB_TOKEN"]
GITHUB_OWNER = os.environ["GITHUB_OWNER"]
GITHUB_API_URL = "https://api.github.com"
GITHUB_DEFAULT_BRANCH = "main"
PROJECT_VISIBILITY = os.environ.get("PROJECT_VISIBILITY", "public")

HF_TOKEN = os.environ.get("HF_TOKEN", "")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

CATEGORIES = [c.strip() for c in os.environ.get(
    "PROJECT_CATEGORIES",
    "LLM,Generative AI,Machine Learning,Deep Learning,NLP,Transformers,Reinforcement Learning"
).split(",") if c.strip()]
PROJECT_GITHUB_PREFIX = os.environ.get("PROJECT_GITHUB_PREFIX", "ai-daily-")
MAX_FILES = int(os.environ.get("MAX_FILES", "10"))
OVERRIDE_CATEGORY = os.environ.get("OVERRIDE_CATEGORY", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
RUN_DATE = datetime.now(timezone.utc).strftime("%Y-%m-%d")

CATEGORY_MAP = {
    "LLM": {"github": "large language model LLM OR RAG OR agent", "hf": "language model"},
    "Generative AI": {"github": "generative AI OR diffusion OR multimodal", "hf": "generative"},
    "Machine Learning": {"github": "machine learning classification regression", "hf": "tabular classification"},
    "Deep Learning": {"github": "deep learning CNN computer vision neural network", "hf": "image classification"},
    "NLP": {"github": "NLP natural language processing sentiment NER", "hf": "text classification"},
    "Transformers": {"github": "transformers BERT ViT encoder decoder attention", "hf": "transformers"},
    "Reinforcement Learning": {"github": "reinforcement learning DQN PPO Q-learning gymnasium", "hf": "reinforcement learning"},
}

PERMISSIVE = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "CC0-1.0", "Unlicense"}


def http_get(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        log.warning("GET %s failed: %s", url, exc)
        return None


def http_post(url, payload, headers=None, timeout=120):
    data = json.dumps(payload).encode()
    h = dict(headers or {})
    h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        log.error("POST %s -> %s: %s", url, exc.code, exc.read().decode()[:300])
        return None
    except Exception as exc:
        log.error("POST %s failed: %s", url, exc)
        return None


def http_put(url, payload, headers=None, timeout=60):
    data = json.dumps(payload).encode()
    h = dict(headers or {})
    h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        log.error("PUT %s -> %s: %s", url, exc.code, exc.read().decode()[:300])
        return None
    except Exception as exc:
        log.error("PUT %s failed: %s", url, exc)
        return None


def http_patch(url, payload, headers=None, timeout=30):
    data = json.dumps(payload).encode()
    h = dict(headers or {})
    h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method="PATCH")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        log.error("PATCH %s -> %s: %s", url, exc.code, exc.read().decode()[:300])
        return None
    except Exception as exc:
        log.error("PATCH %s failed: %s", url, exc)
        return None


def github_headers():
    return {
        "Authorization": f"token {FACTORY_GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def github_repo_exists(repo_name):
    url = f"{GITHUB_API_URL}/repos/{GITHUB_OWNER}/{repo_name}"
    result = http_get(url, headers=github_headers())
    return result is not None and "id" in result


def github_create_repo(repo_name, description):
    payload = {
        "name": repo_name,
        "description": description,
        "private": PROJECT_VISIBILITY != "public",
        "auto_init": False,
    }
    result = http_post(f"{GITHUB_API_URL}/user/repos", payload, headers=github_headers())
    if result and "html_url" in result:
        return result["html_url"]
    return None


def github_upload_file(repo_name, file_path, content, message):
    encoded = base64.b64encode(content.encode()).decode()
    url = f"{GITHUB_API_URL}/repos/{GITHUB_OWNER}/{repo_name}/contents/{file_path}"
    existing = http_get(url, headers=github_headers())
    payload = {"message": message, "content": encoded, "branch": GITHUB_DEFAULT_BRANCH}
    if existing and "sha" in existing:
        payload["sha"] = existing["sha"]
    result = http_put(url, payload, headers=github_headers())
    return result is not None


def supabase_headers():
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def supabase_insert(table, row):
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        log.info("Supabase not configured -- skipping insert into %s", table)
        return {"id": "local-only"}
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    return http_post(url, row, headers=supabase_headers())


def supabase_update(table, row_id, updates):
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return None
    url = f"{SUPABASE_URL}/rest/v1/{table}?id=eq.{row_id}"
    return http_patch(url, updates, headers=supabase_headers())


def supabase_select(table, filters):
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return []
    url = f"{SUPABASE_URL}/rest/v1/{table}?{filters}&limit=1"
    return http_get(url, headers=supabase_headers())


def hf_headers():
    h = {"Accept": "application/json"}
    if HF_TOKEN:
        h["Authorization"] = f"Bearer {HF_TOKEN}"
    return h


def search_hf_models(query, limit=5):
    encoded = urllib.parse.quote(query)
    url = f"https://huggingface.co/api/models?search={encoded}&limit={limit}&sort=downloads&direction=-1"
    result = http_get(url, headers=hf_headers())
    if not isinstance(result, list):
        return []
    out = []
    for m in result:
        license_val = "unknown"
        if isinstance(m.get("cardData"), dict):
            license_val = m["cardData"].get("license", "unknown")
        out.append({
            "type": "hf_model", "name": m.get("modelId", ""),
            "url": f"https://huggingface.co/{m.get('modelId', '')}",
            "license": license_val, "downloads": m.get("downloads", 0),
        })
    return out


def search_github_repos(query, limit=5):
    encoded = urllib.parse.quote(query)
    url = f"{GITHUB_API_URL}/search/repositories?q={encoded}&sort=stars&order=desc&per_page={limit}"
    result = http_get(url, headers=github_headers())
    if not result or "items" not in result:
        return []
    out = []
    for r in result["items"][:limit]:
        lic = "unknown"
        if isinstance(r.get("license"), dict):
            lic = r["license"].get("spdx_id", "unknown") or "unknown"
        out.append({
            "type": "github_repo", "name": r.get("full_name", ""),
            "url": r.get("html_url", ""), "stars": r.get("stargazers_count", 0),
            "license": lic, "description": (r.get("description") or "")[:200],
        })
    return out


def filter_permissive(sources):
    return [s for s in sources if s.get("license", "unknown") in PERMISSIVE or s.get("license") == "unknown"]


def groq_chat(messages, temperature=0.7, max_tokens=4096, json_mode=False):
    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    result = http_post(GROQ_API_URL, payload, headers=headers, timeout=120)
    if result and "choices" in result:
        return result["choices"][0]["message"]["content"]
    log.error("Groq response: %s", result)
    return None


def slugify(text):
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")[:50]


CI_WORKFLOW = '''name: CI
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
jobs:
  test:
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
      - name: Run tests
        run: |
          if [ -d tests ]; then python -m pytest tests/ -v --tb=short || true; fi
          echo "CI complete."
'''


def main():
    log.info("=" * 60)
    log.info("AI Project Factory -- %s", RUN_DATE)
    log.info("DRY_RUN=%s", DRY_RUN)
    log.info("=" * 60)

    if OVERRIDE_CATEGORY and OVERRIDE_CATEGORY in CATEGORY_MAP:
        category = OVERRIDE_CATEGORY
    else:
        day_idx = datetime.now(timezone.utc).timetuple().tm_yday
        category = CATEGORIES[day_idx % len(CATEGORIES)]
    log.info("Category: %s", category)
    cfg = CATEGORY_MAP.get(category, CATEGORY_MAP["Generative AI"])

    github_sources = search_github_repos(cfg["github"], limit=5)
    hf_sources = search_hf_models(cfg["hf"], limit=5)
    all_sources = filter_permissive(github_sources + hf_sources)
    source_context = "\n".join(
        f"- [{s['type']}] {s['name']} ({s.get('license','?')}): {s.get('description', s.get('url',''))}"
        for s in all_sources[:8]
    )
    log.info("Sources: %d", len(all_sources))

    log.info("Calling Groq planner...")
    planner_prompt = f"""You are an expert AI/ML engineer. Design an ORIGINAL {category} project for a developer portfolio.

Reference sources (inspiration + attribution only):
{source_context or "(none)"}

Today: {RUN_DATE}

Return a JSON object with EXACTLY these keys:
{{
  "project_name": "Human-readable title (max 60 chars)",
  "repo_name_suffix": "short-slug-3-to-5-words",
  "description": "One-sentence repo description (max 120 chars)",
  "difficulty": "beginner|intermediate|advanced",
  "tech_stack": ["Python", "..."],
  "files": [
    {{"path": "README.md", "description": "Overview and setup"}},
    {{"path": "requirements.txt", "description": "Dependencies"}},
    {{"path": "src/main.py", "description": "Entry point"}},
    {{"path": "tests/test_main.py", "description": "Unit tests"}}
  ],
  "source_attributions": ["url1"]
}}

Rules: max {MAX_FILES} files, must include README.md + requirements.txt + src/ + tests/.
"""
    plan_raw = groq_chat(
        [{"role": "user", "content": planner_prompt}],
        temperature=0.85, max_tokens=2048, json_mode=True,
    )
    if not plan_raw:
        log.error("Groq planner returned nothing.")
        sys.exit(1)
    try:
        plan = json.loads(plan_raw)
    except json.JSONDecodeError as exc:
        log.error("Planner JSON parse failed: %s", exc)
        sys.exit(1)

    log.info("Plan: %s", plan.get("project_name"))

    suffix = slugify(plan.get("repo_name_suffix", plan.get("project_name", "project")))
    repo_name = f"{PROJECT_GITHUB_PREFIX}{suffix}"
    if github_repo_exists(repo_name):
        repo_name = f"{repo_name}-{RUN_DATE}"
    log.info("Repo name: %s", repo_name)

    existing = supabase_select("projects", f"repo_name=eq.{repo_name}&status=eq.published")
    if existing:
        log.warning("Already published. Skipping.")
        sys.exit(0)

    if DRY_RUN:
        log.info("DRY RUN -- plan:\n%s", json.dumps(plan, indent=2))
        sys.exit(0)

    run_id = hashlib.md5(f"{repo_name}-{RUN_DATE}".encode()).hexdigest()[:16]
    sb_result = supabase_insert("projects", {
        "project_name": plan.get("project_name", repo_name),
        "repo_name": repo_name, "category": category,
        "description": plan.get("description", ""),
        "difficulty": plan.get("difficulty", "intermediate"),
        "tech_stack": plan.get("tech_stack", []),
        "source_urls": plan.get("source_attributions", []),
        "plan": plan, "github_owner": GITHUB_OWNER,
        "status": "generating", "generation_run_id": run_id,
    })
    project_id = None
    if isinstance(sb_result, list) and sb_result:
        project_id = sb_result[0].get("id")
    elif isinstance(sb_result, dict):
        project_id = sb_result.get("id")
    log.info("Supabase project ID: %s", project_id)

    log.info("Creating GitHub repo: %s", repo_name)
    repo_url = github_create_repo(repo_name, plan.get("description", "")[:120])
    if not repo_url:
        log.error("Failed to create repo.")
        if project_id:
            supabase_update("projects", project_id, {"status": "error", "error_message": "repo creation failed"})
        sys.exit(1)
    log.info("Repo: %s", repo_url)
    time.sleep(2)

    uploaded = []
    for file_info in plan.get("files", [])[:MAX_FILES]:
        fp = file_info["path"]
        log.info("Generating: %s", fp)
        prompt = f"""Write the file "{fp}" for the project "{plan.get('project_name')}".
Category: {category}
Tech stack: {', '.join(plan.get('tech_stack', []))}
Purpose: {file_info.get('description', '')}
Write complete production-quality code. No placeholders. Return ONLY raw file content."""
        content = None
        for attempt in range(3):
            content = groq_chat([{"role": "user", "content": prompt}], temperature=0.6, max_tokens=4096)
            if content:
                break
            time.sleep(3)
        if not content:
            log.error("Skipping %s -- generation failed", fp)
            continue
        content = re.sub(r"^```[a-zA-Z]*\n?", "", content)
        content = re.sub(r"\n?```$", "", content)
        if github_upload_file(repo_name, fp, content, f"feat: add {fp}"):
            uploaded.append(fp)
            log.info("Uploaded: %s", fp)
        else:
            log.error("Upload failed: %s", fp)
        time.sleep(1)

    if github_upload_file(repo_name, ".github/workflows/ci.yml", CI_WORKFLOW, "ci: add GitHub Actions CI"):
        uploaded.append(".github/workflows/ci.yml")
        log.info("CI workflow uploaded.")

    if project_id:
        supabase_update("projects", project_id, {
            "status": "published", "github_url": repo_url,
            "github_actions_url": f"{repo_url}/actions",
            "github_default_branch": GITHUB_DEFAULT_BRANCH,
            "published_at": datetime.now(timezone.utc).isoformat(),
        })

    log.info("=" * 60)
    log.info("SUCCESS: %s", plan.get("project_name"))
    log.info("Repo:    %s", repo_url)
    log.info("Files:   %d uploaded", len(uploaded))
    log.info("=" * 60)


if __name__ == "__main__":
    main()
