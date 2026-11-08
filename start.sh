#!/usr/bin/env bash
# Запуск одной командой:  bash start.sh
# Ставит зависимости и запускает интерфейс. Ссылку API и ключ модели вводите в браузере: ⚙ Настройки.
set -e
cd "$(dirname "$0")"
mkdir -p runs
exec > >(tee -a runs/start.log) 2>&1
echo "=== $(date) start.sh"
if [ ! -x .venv/bin/python ]; then
  echo "Создаю окружение Python..."
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q -r requirements.txt
echo
echo "Интерфейс: http://localhost:8000   (не закрывайте это окно; остановить — Ctrl+C)"
( sleep 2; open http://localhost:8000 2>/dev/null || true ) &
python -m uvicorn server.app:app --port 8000 --reload --reload-dir cqa --reload-dir server
