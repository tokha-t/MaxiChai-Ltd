#!/usr/bin/env bash
# Контрольный прогон за одну команду: оценка → проверка формата → commit → push.
# Использование: ./scripts/prod_run.sh <group_id> --checklist <ID принятого профиля>
# Дополнительно: --file <JSON> | --url <ссылка API>, --judges <судьи>, --parallel <N>.
# Число проверок (3), группа, путь сдачи и ожидаемые 10 записей не переопределяются.
set -euo pipefail
cd "$(dirname "$0")/.."
GROUP="${1:?Укажите group_id контрольного набора и --checklist ID принятого профиля}"
shift
CHECKLIST=""
SEEN=""
EXTRA=()
forbidden() { echo "$1" >&2; exit 2; }
[[ "$GROUP" != --* ]] || forbidden "Первым аргументом нужен group_id контрольного набора"
while [[ $# -gt 0 ]]; do
  option="$1"
  case "$option" in
    --checklist|--file|--url|--judges|--parallel) ;;
    *) forbidden "Недопустимый параметр: $option. Режим сдачи фиксирует --votes 3, --expect 10 и путь результата." ;;
  esac
  [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || forbidden "Для $option нужно значение"
  case " $SEEN " in *" $option "*) forbidden "Параметр $option указан повторно" ;; esac
  SEEN="$SEEN $option"
  if [[ "$option" == --checklist ]]; then
    [[ "$2" =~ ^[1-9][0-9]*$ ]] || forbidden "--checklist должен быть числовым ID принятой версии"
    CHECKLIST="$2"
  else
    if [[ "$option" == --parallel ]]; then
      [[ "$2" =~ ^[1-9][0-9]*$ ]] || forbidden "--parallel должен быть положительным целым числом"
    fi
    EXTRA+=("$option" "$2")
  fi
  shift 2
done
[[ -n "$CHECKLIST" ]] || forbidden "Укажите --checklist ID принятого профиля; active/default для контрольного запуска не используются"
if [[ " $SEEN " == *" --file "* && " $SEEN " == *" --url "* ]]; then
  forbidden "Выберите один источник: --file или --url"
fi
[ -x .venv/bin/python ] && source .venv/bin/activate
python -m cqa evaluate --group "$GROUP" --checklist "$CHECKLIST" --votes 3 --refresh \
  --out results/prod/results.json --expect 10 ${EXTRA[@]+"${EXTRA[@]}"}
python -m cqa validate results/prod/results.json --group "$GROUP" --expect 10
git add results/prod/results.json
git commit -m "PROD results: $GROUP"
git push
echo "Готово: results/prod/results.json отправлен. Открой репозиторий на GitHub и проверь файл."
