#!/usr/bin/env bash
# Update (or roll back) the running bot.
#
#   deploy/update.sh               pull the current tag again (or rebuild, in local-build mode)
#   deploy/update.sh sha-1a2b3c4   deploy that image from GHCR (also used for rollbacks)
#   deploy/update.sh latest | v1.2.0
#
# GitHub Actions runs it over SSH through a forced command in the deploy user's
# authorized_keys; the tag then arrives in SSH_ORIGINAL_COMMAND.
set -euo pipefail

ROOT=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)
# shellcheck source=deploy/lib.sh
source "$ROOT/deploy/lib.sh"
cd "$ROOT"
ENV_FILE="$ROOT/.env"
TAG_RE='^(latest|sha-[0-9a-f]{7,40}|v[0-9][0-9A-Za-z.+-]*)$'

[[ -f $ENV_FILE ]] || die "No existe $ENV_FILE: ejecuta primero deploy/install.sh"

tag=${1:-${SSH_ORIGINAL_COMMAND:-}}
tag=${tag//[[:space:]]/}
if [[ -n $tag && ! $tag =~ $TAG_RE ]]; then
	die "Etiqueta no válida: '$tag' (usa latest, sha-xxxxxxx o vX.Y.Z)"
fi

# One update at a time (CI deploys can overlap with a manual run or the backup cron).
mkdir -p "$ROOT/.state"
lock="$ROOT/.state/update.lock"
[[ -e $lock ]] || : >"$lock"
exec 9<"$lock" # read-only is enough for flock, whoever created the file
flock -w 900 9 || die "Hay otra actualización en curso"

previous_tag=$(env_get "$ENV_FILE" BOT_TAG)
previous_tag=${previous_tag:-latest}
previous_compose_file=$(env_get "$ENV_FILE" COMPOSE_FILE)
image=$(env_get "$ENV_FILE" BOT_IMAGE)
image=${image:-$BOT_IMAGE_DEFAULT}
# The image the bot runs now. "pull latest" or a local rebuild re-points the same tag to the
# new image, so a rollback has to re-tag this id; restoring the tag name alone is not enough.
previous_image_id=$(docker compose images -q bot 2>/dev/null | head -n 1) || true

bot_image_ref() { # the image reference compose uses for the bot with the current .env
	if uses_local_build; then
		echo telebot-photos:local
	else
		echo "${image}:$(env_get "$ENV_FILE" BOT_TAG)"
	fi
}

restore_previous() {
	warn "Volviendo a la versión anterior ($previous_tag)…"
	env_set "$ENV_FILE" BOT_TAG "$previous_tag"
	if [[ -n $previous_compose_file ]]; then
		env_set "$ENV_FILE" COMPOSE_FILE "$previous_compose_file"
	else
		env_unset "$ENV_FILE" COMPOSE_FILE
	fi
	if [[ -n $previous_image_id ]]; then
		docker tag "$previous_image_id" "$(bot_image_ref)" || true
	fi
	docker compose up -d --remove-orphans --wait --wait-timeout 180 ||
		warn "La versión anterior tampoco arrancó: revisa 'docker compose logs bot'"
}

# The other images only change with the compose files: a failed pull (e.g. a Docker Hub rate
# limit) must not block the update, "up" pulls whatever is really missing.
pull_others() {
	local services
	mapfile -t services < <(helper_services)
	docker compose pull --quiet --ignore-pull-failures "${services[@]}" >/dev/null 2>&1 ||
		warn "No se pudieron actualizar las imágenes de ${services[*]}; sigo con las que hay"
}

pull_failed_hint() {
	warn "Si el paquete de GHCR es privado, hazlo público o inicia sesión como el usuario"
	warn "que despliega ($(id -un)): docker login ghcr.io (ver DEPLOY.md, apartado 7)."
}

if docker compose ps --status running --services 2>/dev/null | grep -qx bot; then
	"$ROOT/deploy/backup.sh" --quiet || warn "No se pudo hacer la copia de seguridad previa"
fi

repo_update || warn "No se pudo actualizar el repositorio (git); sigo con los ficheros actuales"

if [[ -n $tag ]]; then
	# An explicit tag always comes from GHCR: leave local-build mode if it was on.
	env_set "$ENV_FILE" BOT_TAG "$tag"
	set_compose_file "$(flag uses_local_bot_api)" 0
	info "Descargando ${image}:${tag}…"
	if ! docker compose pull --quiet bot; then
		pull_failed_hint
		restore_previous
		die "No se pudo descargar la imagen ${tag}"
	fi
	pull_others
elif uses_local_build; then
	info "Construyendo la imagen del bot en este servidor…"
	pull_others
	if ! docker compose build bot; then
		die "Falló la construcción de la imagen; el bot sigue con la versión anterior"
	fi
else
	info "Descargando ${image}:${previous_tag}…"
	if ! docker compose pull --quiet bot; then
		pull_failed_hint
		die "No se pudo descargar la imagen ${previous_tag}; el bot sigue con la versión anterior"
	fi
	pull_others
fi

info "Reiniciando los servicios…"
if ! docker compose up -d --remove-orphans --wait --wait-timeout 180; then
	docker compose logs --tail 40 bot >&2 || true
	restore_previous
	die "La nueva versión no arrancó correctamente"
fi

# Old sha-* images stay tagged after each deploy: remove every bot image no container uses
# (a later rollback pulls it from GHCR again), then dangling layers.
docker image prune -af --filter "label=org.opencontainers.image.source=https://github.com/jpchaconr/telebot-photos" >/dev/null || true
docker image prune -f >/dev/null || true
version=$(docker compose exec -T bot python -c \
	"import json, os, urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('WEB_PORT', '8080')))['version'])" \
	2>/dev/null) || version=desconocida
ok "Bot actualizado (versión ${version})"
