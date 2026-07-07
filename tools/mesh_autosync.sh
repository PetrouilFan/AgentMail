#!/usr/bin/env bash
# AgentMail mesh automation — runs on a schedule.
# 1. Regenerate mesh.yaml from the live hermes keyring (fresh roster for sharing).
# 2. Keep the hermes<->juno ping-pong loop alive (restart if not running).
# 3. If the AgentMail repo has tracked changes, commit + push them (no prompt).
# mesh.yaml is gitignored, so it's refreshed locally, not committed.
set -u
REPO="$HOME/Projects/AgentMail"
cd "$REPO" || exit 1
source .venv/bin/activate 2>/dev/null || true

# 1. keep mesh.yaml fresh
python tools/gen_mesh.py ~/.agentmail/config.yaml mesh.yaml >/dev/null 2>&1

# 2. keep ping-pong loop alive
if ! pgrep -f "pingpong_hermes.py" >/dev/null 2>&1; then
  nohup python pingpong_hermes.py >/tmp/pingpong_hermes.log 2>&1 &
fi

# 3. commit + push any tracked repo changes (generator, docs, etc.)
if ! git diff --quiet || ! git diff --cached --quiet || [ -n "$(git status --porcelain)" ]; then
  git add -A
  if git diff --cached --quiet; then
    exit 0
  fi
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  git commit -q -m "auto: mesh automation sync ($ts)" && git push -u origin main >/dev/null 2>&1
  echo "pushed at $ts"
fi
