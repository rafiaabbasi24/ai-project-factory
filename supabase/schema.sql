create extension if not exists pgcrypto;

create table if not exists public.projects (
  id uuid primary key default gen_random_uuid(),
  project_name text not null,
  repo_name text not null unique,
  category text not null,
  description text,
  difficulty text,
  tech_stack jsonb default '[]'::jsonb,
  source_urls jsonb default '[]'::jsonb,
  plan jsonb,
  github_owner text,
  github_url text,
  github_default_branch text,
  github_actions_url text,
  status text not null default 'planned',
  error_message text,
  generation_run_id text,
  published_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.sources (
  id uuid primary key default gen_random_uuid(),
  project_id uuid references public.projects(id) on delete cascade,
  source_type text not null,
  source_name text,
  source_url text,
  license text,
  metadata jsonb default '{}'::jsonb,
  retrieved_at timestamptz not null default now()
);

create table if not exists public.generation_runs (
  id uuid primary key default gen_random_uuid(),
  project_id uuid references public.projects(id) on delete cascade,
  provider text not null default 'groq',
  model text,
  stage text,
  prompt_hash text,
  status text not null default 'started',
  output_metadata jsonb,
  error_message text,
  started_at timestamptz not null default now(),
  finished_at timestamptz
);

create index if not exists idx_projects_category on public.projects(category);
create index if not exists idx_projects_status on public.projects(status);
create index if not exists idx_projects_created_at on public.projects(created_at desc);
create index if not exists idx_sources_project_id on public.sources(project_id);
create index if not exists idx_generation_runs_project_id on public.generation_runs(project_id);

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists trg_projects_updated_at on public.projects;
create trigger trg_projects_updated_at
before update on public.projects
for each row execute function public.set_updated_at();
