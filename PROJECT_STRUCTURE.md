# Project structure

```text
ai-project-factory/
├── .env.example
├── .gitignore
├── docker-compose.yml
├── README.md
├── PROJECT_STRUCTURE.md
├── workflows/
│   └── daily-ai-project-factory.json
├── supabase/
│   └── schema.sql
├── docs/
│   └── workflow-overview.md
└── scripts/
    ├── setup.ps1
    ├── start.bat
    ├── stop.bat
    └── validate_workflow.py
```

The actual credentials file is intentionally not included. Copy `.env.example` to `.env` locally and paste your credentials there.
