#!/usr/bin/env bash
# Point the DuckDNS domain in .env at this server's public IPv4 (run hourly by cron).
#
#   deploy/duckdns-update.sh [IP]     (without IP, DuckDNS uses the address of the request)
set -euo pipefail

ROOT=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)
# shellcheck source=deploy/lib.sh
source "$ROOT/deploy/lib.sh"

domain=$(env_get "$ROOT/.env" DOMAIN)
token=$(env_get "$ROOT/.env" DUCKDNS_TOKEN)
if [[ $domain != *.duckdns.org ]]; then
	exit 0 # not using DuckDNS
fi
[[ -n $token ]] || die "Falta DUCKDNS_TOKEN en .env"

# "fotos.duckdns.org" and "bot.fotos.duckdns.org" both belong to the "fotos" record.
name=${domain%.duckdns.org}
name=${name##*.}
ip=${1:-}

response=$(curl_secret \
	"https://www.duckdns.org/update?domains=${name}&token=${token}&ip=${ip}" \
	-4 -fsS --max-time 20) || die "No se pudo contactar con DuckDNS"
[[ $response == OK* ]] || die "DuckDNS rechazó la actualización (¿token o subdominio incorrectos?)"
