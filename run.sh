#!/usr/bin/env bash
# Fleet Copilot, one-command start.   ./run.sh   ->  http://localhost:8000
set -e
cd "$(dirname "$0")"
if [ -f .env ]; then set -a; . ./.env; set +a; fi
PY=${PYTHON:-python3}
if [ ! -x .venv/bin/python ]; then
  echo "▶ Creating virtualenv…"
  if ! $PY -m venv .venv 2>/dev/null; then
    echo "✖ python3-venv is missing. Run:  sudo apt update && sudo apt install -y python3-venv python3-pip   then ./run.sh again"
    exit 1
  fi
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q -r requirements.txt
fi
./.venv/bin/python -c "import websockets.sync.client" 2>/dev/null || ./.venv/bin/pip install -q "websockets>=12"
if [ ! -f reports/comparison.md ] || [ ! -f reports/chaos.json ] || [ ! -f reports/test_cases.md ]; then
  echo "▶ Generating proof: agent test cases + 1000-farm chaos test + 100-farm benchmark (≈30-60 s)…"
  FLEET_LLM_PROVIDER=none ./.venv/bin/python -m fleet.agents.testcases > /dev/null
  ./.venv/bin/python -m fleet.bench --chaos 1000
  ./.venv/bin/python -m fleet.bench --seeds 100 > /dev/null
fi
[ -n "$FLEET_AGOPENWEB" ] && echo "▶ AgOpenWeb twin: $FLEET_AGOPENWEB (tractor ${FLEET_AGOPENWEB_TRACTOR:-T1})"
echo "▶ Agent LLM chain:"; ./.venv/bin/python -c "from fleet.agents.llm_tools import configs; c=configs(); print('   ' + (' -> '.join(x['name']+':'+x['model'] for x in c) + ' -> rules' if c else 'none configured -> deterministic rules (set a key in .env or start Ollama)'))"
exec ./.venv/bin/python -m fleet.server
