#!/usr/bin/env bash
# Контрольный прогон за одну команду: оценка → проверка формата → commit → push.
# Использование:  ./scripts/prod_run.sh <group_id контрольного набора>
#           или:  ./scripts/prod_run.sh <group_id> --file путь/к/prod.json   (если набор выдали файлом)
#           или:  ./scripts/prod_run.sh <group_id> --url "<новая ссылка без group_id>"
set -euo pipefail
cd "$(dirname "$0")/.."
GROUP="${1:?Укажите group_id контрольного набора}"
shift || true
[ -x .venv/bin/python ] && source .venv/bin/activate
python -m cqa evaluate --group "$GROUP" --refresh --out results/prod/results.json --expect 10 "$@"
python -m cqa validate results/prod/results.json --group "$GROUP" --expect 10
git add results/prod/results.json
git commit -m "PROD results: $GROUP"
git push
echo "Готово: results/prod/results.json отправлен. Открой репозиторий на GitHub и проверь файл."
