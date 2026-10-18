#!/usr/bin/env bash
# Consistent copy of the bot database into backups/ (keeps the last TELEBOT_BACKUP_KEEP).
#
#   deploy/backup.sh [--quiet]
#
# The database uses WAL mode, so copying bot.db by hand can lose data: this uses
# "VACUUM INTO" inside the running container instead. The copy holds the chats'
# credentials encrypted with ENCRYPTION_KEY: store .env (the key) somewhere else.
set -euo pipefail

ROOT=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)
# shellcheck source=deploy/lib.sh
source "$ROOT/deploy/lib.sh"
cd "$ROOT"

quiet=0
[[ ${1:-} == --quiet ]] && quiet=1
keep=${TELEBOT_BACKUP_KEEP:-14}
stamp=$(date -u +%Y%m%d-%H%M%S)
inside="/data/backup-${stamp}.db"
target="$ROOT/backups/bot-${stamp}.db"

mkdir -p "$ROOT/backups"
chmod 700 "$ROOT/backups" 2>/dev/null || true

docker compose exec -T bot python - "$inside" <<'PY'
import os
import sqlite3
import sys

source = sqlite3.connect(os.environ.get("DATABASE_PATH", "/data/bot.db"))
source.execute("VACUUM INTO ?", (sys.argv[1],))
source.close()
PY
if ! copy_output=$(docker compose cp "bot:${inside}" "$target" 2>&1); then
	die "No se pudo copiar la base de datos: $copy_output"
fi
docker compose exec -T bot rm -f "$inside"
chmod 600 "$target"

# Rotation: names sort by date (bot-YYYYmmdd-HHMMSS.db); delete all but the newest $keep.
mapfile -t old < <(find "$ROOT/backups" -maxdepth 1 -name 'bot-*.db' -printf '%f\n' |
	sort -r | tail -n +"$((keep + 1))" | sed "s|^|$ROOT/backups/|")
if ((${#old[@]})); then
	rm -f -- "${old[@]}"
fi

if ((!quiet)); then
	ok "Copia guardada en $target"
	info "Recuerda guardar también .env (contiene ENCRYPTION_KEY) en otro sitio seguro."
fi
