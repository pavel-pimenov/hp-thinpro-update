#!/usr/bin/env bash
# Deploy HP ThinPro Update mirror on a fresh Debian/Ubuntu VM.
# Usage:  bash scripts/deploy.sh
set -euo pipefail

REPO_URL="${1:-https://github.com/pavel-pimenov/hp-thinpro-update.git}"
APP_DIR="${APP_DIR:-/opt/hp-thinpro-update}"
PORT="${PORT:-8080}"
SECRET="${SECRET:-$(openssl rand -hex 32)}"

echo "==> Проверка/установка Docker + compose-плагина"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker "$USER"
  echo "  Выйдите и зайдите заново, чтобы группа docker применилась (или запустите скрипт через sudo)."
fi
docker compose version >/dev/null 2>&1 || echo "  Нужен compose-плагин: sudo apt-get install -y docker-compose-plugin || sudo apt install -y docker-compose-v2"

echo "==> Клонирование приложения"
sudo mkdir -p "$APP_DIR" && sudo git clone "$REPO_URL" "$APP_DIR"
cd "$APP_DIR"

echo "==> Настройка .env"
sudo tee .env >/dev/null <<EOF
PORT=$PORT
SECRET_KEY=$SECRET
TZ=UTC
EOF

echo "==> Сборка и запуск (первый каталог подтянется с ftp.hp.com при старте)"
sudo docker compose up -d --build

echo ""
echo "Готово в http://$(hostname -I | awk '{print $1}'):$PORT"
echo "Проверка:"
echo "  curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:$PORT/healthz"
echo "  curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:$PORT/api/index.json"
echo "Скачка живого пакета:"
echo "  curl -OJ -L http://127.0.0.1:$PORT/file/addons/2/0"
echo "Логи: sudo docker compose logs -f web"