#!/usr/bin/env bash
# Установка на VPS (Aeza, Ubuntu/Debian). Запуск от root:
#   curl -fsSL https://raw.githubusercontent.com/savvatarasoff-a11y/bot-stat/main/deploy/install.sh | bash -s -- ТОКЕН
# Повторный запуск обновляет бота до последней версии из GitHub (база сохраняется).
set -euo pipefail

REPO="https://github.com/savvatarasoff-a11y/bot-stat"
DIR=/opt/bot-stat
TOKEN="${1:-}"

apt-get update -qq
apt-get install -y -qq git python3 python3-venv >/dev/null

if [ -d "$DIR/.git" ]; then
    git -C "$DIR" pull --ff-only
else
    git clone "$REPO" "$DIR"
fi

python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --upgrade pip
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"
# TikTok часто меняет сайт: при каждом обновлении берём свежий yt-dlp
"$DIR/.venv/bin/pip" install -q --upgrade "yt-dlp[default,curl-cffi]"

if [ -n "$TOKEN" ]; then
    echo "BOT_TOKEN=$TOKEN" > "$DIR/.env"
elif [ ! -f "$DIR/.env" ]; then
    echo "Передайте токен: bash install.sh ТОКЕН" >&2
    exit 1
fi
chmod 600 "$DIR/.env"

cp "$DIR/deploy/bot-stat.service" /etc/systemd/system/bot-stat.service
systemctl daemon-reload
systemctl enable --now bot-stat
systemctl restart bot-stat
sleep 2
systemctl --no-pager status bot-stat | head -5
echo "Готово. Логи: journalctl -u bot-stat -f"
