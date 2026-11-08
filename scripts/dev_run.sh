#!/usr/bin/env bash
# Оценка всего DEV одним голосом (экономит бесплатную квоту) → results/dev/results.json
set -e
cd "$(dirname "$0")/.."
[ -x .venv/bin/python ] && source .venv/bin/activate
python -m cqa evaluate --group DEV --votes "${VOTES:-1}" --out results/dev/results.json "$@" 2>&1 | tee -a runs/dev_run.log
