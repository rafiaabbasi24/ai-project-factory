$ErrorActionPreference = 'Stop'

if (-not (Test-Path '.env')) {
  Copy-Item '.env.example' '.env'
  Write-Host 'Created .env from .env.example. Paste your credentials into .env, then run this script again.' -ForegroundColor Yellow
  exit 0
}

docker compose up -d
Write-Host 'Waiting for n8n to become healthy...' -ForegroundColor Cyan
Start-Sleep -Seconds 15

$health = $false
for ($i = 0; $i -lt 30; $i++) {
  try {
    $r = Invoke-WebRequest -UseBasicParsing -Uri 'http://localhost:5678/healthz' -TimeoutSec 3
    if ($r.StatusCode -eq 200) { $health = $true; break }
  } catch {}
  Start-Sleep -Seconds 2
}

if (-not $health) {
  Write-Host 'n8n did not become healthy. Run: docker compose logs -f n8n' -ForegroundColor Red
  exit 1
}

Write-Host 'n8n is running at http://localhost:5678' -ForegroundColor Green
Write-Host 'Import workflows/daily-ai-project-factory.json from the n8n UI.' -ForegroundColor Green
Write-Host 'Run the Manual Test trigger once before activating the daily trigger.' -ForegroundColor Green
