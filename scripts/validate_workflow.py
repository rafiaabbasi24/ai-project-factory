import json
from pathlib import Path

path = Path(__file__).resolve().parents[1] / 'workflows' / 'daily-ai-project-factory.json'
data = json.loads(path.read_text(encoding='utf-8'))

nodes = data.get('nodes', [])
names = {n['name'] for n in nodes}
ids = {n['id'] for n in nodes}
assert data.get('name'), 'Workflow name missing'
assert len(nodes) >= 20, f'Unexpectedly small workflow: {len(nodes)} nodes'
required = [
    'Manual Test', 'Daily 08:00 IST', 'Topic Router', 'GitHub Search',
    'HF Models Search', 'HF Datasets Search', 'Merge Source Results',
    'Source Pack', 'Groq Planner', 'Supabase Repo Check',
    'GitHub Create Repository', 'Groq File Writer', 'Upload File to GitHub',
    'Supabase Final Update'
]
for name in required:
    assert name in names, f'Missing node: {name}'

for source, payload in data.get('connections', {}).items():
    assert source in names, f'Unknown source node: {source}'
    for branch in payload.get('main', []):
        for target in branch:
            assert target['node'] in ids, f"Unknown target id from {source}: {target['node']}"

print(f'OK: {len(nodes)} nodes, {len(data.get("connections", {}))} connected sources')
