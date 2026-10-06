# AI Daily Project Factory — n8n + Groq + Hugging Face + GitHub + Supabase

A self-hosted automation system that creates **one original AI/ML portfolio project every day** and publishes it to GitHub.

## What it automates

1. Rotates through LLM, Generative AI, Machine Learning, Deep Learning, NLP, Transformers and Reinforcement Learning.
2. Searches GitHub and Hugging Face for current/reference material.
3. Filters source metadata and keeps source links/licensing context.
4. Uses Groq to design an original project and file plan.
5. Generates files one-by-one with Groq to keep generations manageable.
6. Creates a new GitHub repository.
7. Uploads the complete project, including a generic GitHub Actions CI workflow.
8. Stores project metadata, sources and generation status in Supabase.
9. Leaves GitHub Actions to validate the generated repository.
10. Optionally sends a Telegram message after publishing.

## Architecture

```text
n8n Schedule
   -> Topic Router
   -> GitHub Search + Hugging Face Models + Hugging Face Datasets
   -> Source Pack / License Filter
   -> Supabase Duplicate Check
   -> Groq Project Planner
   -> Repo Collision Check
   -> Supabase Project Record
   -> GitHub Create Repository
   -> Groq File Writer (per file)
   -> GitHub Contents API (per file)
   -> GitHub Actions CI
   -> Supabase Final Status
   -> Optional Telegram notification
```

## Important design choice

The factory **does not blindly copy repositories**. It uses public source metadata, references, datasets, models and ideas as context, and instructs the model to create an original implementation with source attribution. The GitHub search stage only passes source entries with a known permissive license to the planner when a license is present.

## Prerequisites

- Docker Desktop
- A Groq API key
- A GitHub token with permission to create repositories and write repository contents
- A Supabase project and service-role key
- Hugging Face token is optional for public search, but recommended

GitHub's REST API supports creating repositories and creating/updating repository files through the REST API: https://docs.github.com/en/rest/repos and https://docs.github.com/en/rest/repos/contents

Hugging Face exposes API endpoints for models and datasets and programmatic Hub search: https://huggingface.co/docs/hub/api and https://huggingface.co/docs/huggingface_hub/guides/search

Groq's API uses an OpenAI-compatible chat-completions endpoint and supports JSON/structured outputs; the model can be changed through `GROQ_MODEL`: https://console.groq.com/docs/api-reference

## 1. Configure credentials

A `.env` template is already included in the folder. Paste your real credentials into `.env` locally. You can also recreate it from `.env.example` if needed.

Then paste your values into `.env` locally. **Do not paste real secrets into GitHub or commit `.env`.**

Minimum required:

```env
GROQ_API_KEY=...
GITHUB_TOKEN=...
GITHUB_OWNER=...
SUPABASE_URL=https://YOUR_PROJECT.supabase.co
SUPABASE_SERVICE_ROLE_KEY=...
```

## 2. Create the Supabase tables

Open Supabase SQL Editor and run:

```text
supabase/schema.sql
```

## 3. Start n8n — no local n8n installation required

From this folder:

```bash
docker compose up -d
```

Then open:

```text
http://localhost:5678
```

Create your local n8n owner account on first launch.

## 4. Import the workflow

Import this file into n8n:

```text
workflows/daily-ai-project-factory.json
```

Then activate the workflow.

The workflow contains both a **Manual Test** trigger and a **Daily Schedule** trigger. Use Manual Test first to verify your credentials and GitHub/Supabase setup. After it succeeds, keep the Daily Schedule trigger active.

## 5. Daily schedule

Default:

```text
08:00 Asia/Kolkata every day
```

The schedule value is stored directly in the workflow so it is easy to edit. `PUBLISH_HOUR` and `PUBLISH_MINUTE` are also included in `.env` as configuration reference.

## 6. Output

Each successful run creates a GitHub repository such as:

```text
ai-daily-transformer-text-classifier
ai-daily-rag-document-copilot
ai-daily-dqn-cartpole
```

Each repository is intended to contain:

```text
project/
├── README.md
├── requirements.txt / package.json
├── src/ or app/
├── tests/
├── .gitignore
├── .github/
│   └── workflows/
│       └── ci.yml
└── optional config files
```

## 7. Troubleshooting

### Groq errors

Set `GROQ_MODEL` to a model currently available on your Groq account. Groq maintains a current model list in its documentation: https://console.groq.com/docs/models

### GitHub 401/403

Check `GITHUB_TOKEN`, repository permissions and `GITHUB_OWNER`. The workflow creates the repository under the authenticated account using the GitHub REST API: https://docs.github.com/en/rest/repos/repos

### Supabase 401/403

Use the project's service-role key in the local `.env`. Do not expose that key in frontend code.

### GitHub upload conflict

The factory intentionally creates an empty repository first, then initializes it by adding the first generated file. GitHub documents using the contents API to initialize an empty repository: https://docs.github.com/en/rest/guides/using-the-rest-api-to-interact-with-the-git-database

## Security

- Never commit `.env`.
- Keep the GitHub token and Supabase service-role key server-side.
- For production deployment, put secrets into the deployment platform's secret store.
- Review generated repositories before enabling public distribution for sensitive or licensed material.

## Project generated by this factory

The factory is designed to be extended with:

- more source APIs
- research/paper discovery
- better license checks
- code execution sandboxes
- PR-based publishing
- project ranking
- automated issue creation
- deployment to Vercel/Render/Hugging Face Spaces
