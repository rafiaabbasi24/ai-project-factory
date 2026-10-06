# Workflow map

```text
Manual Test OR Daily 08:00
          |
          v
    Topic Router
          |
     +----+-----+
     |    |     |
   GitHub HF Models HF Datasets
     |    |     |
     +----+-----+
          |
      Source Pack
          |
   Supabase Duplicate Check
          |
      Groq Planner
          |
      Parse Plan
          |
     Repo Name Check
          |
    Supabase Insert
          |
 GitHub Create Repository
          |
  Prepare File Queue
          |
     Split Out Files
          |
   Groq File Generator
          |
 GitHub Contents Upload
          |
  Finalize Upload/Status
          |
   GitHub Actions (CI)
          |
   Supabase Final Update
          |
   Optional Telegram
```
