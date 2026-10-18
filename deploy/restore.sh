#!/usr/bin/env bash
# Restore the bot database from a file made by deploy/backup.sh.
#
#   deploy/restore.sh backups/bot-20261018-031700.db
#
# The .env in use must hold the same ENCRYPTION_KEY the backup was made with.
set -euo pipefail

ROOT=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)
# shellcheck source=deploy/lib.sh
source "$ROOT/deploy/lib.sh"
cd "$ROOT"

[[ $# -eq 1 ]] || die "Uso: deploy/restore.sh FICHERO.db"
file=$(readlink -f "$1")
[[ -f $file ]] || die "No existe $1"
[[ $(head -c 15 "$file") == "SQLite format 3" ]] || die "$1 no es una base de datos SQLite"

# Work on a private copy: the backup made just below may rotate the requested file away.
src=$(mktemp "$ROOT/.state/restore.XXXXXX")
trap 'rm -f "$src"' EXIT
cp -- "$file" "$src"

if docker compose ps --status running --services 2>/dev/null | grep -qx bot; then
	info "Copia de seguridad del estado actual antes de restaurar…"
	"$ROOT/deploy/backup.sh"
fi

info "Parando el bot…"
docker compose stop bot

info "Restaurando $file…"
# The file travels on stdin. The current database is only replaced once the new copy is
# complete, and the WAL files go too: an old -wal next to the restored database corrupts it.
docker compose run --rm -T --no-deps --user 0 --entrypoint sh bot -c '
	set -e
	cat >/data/bot.db.new
	[ -s /data/bot.db.new ]
	chown 101:101 /data/bot.db.new
	chmod 600 /data/bot.db.new
	rm -f /data/bot.db-wal /data/bot.db-shm
	mv /data/bot.db.new /data/bot.db
' <"$src"

docker compose up -d --wait --wait-timeout 180 bot
ok "Base de datos restaurada y bot en marcha"
