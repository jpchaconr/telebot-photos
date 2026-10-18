# shellcheck shell=bash
# Helpers shared by the deploy/*.sh scripts. Source it after setting ROOT (repo checkout).

if [[ -t 2 ]]; then
	_C_INFO=$'\033[1;34m' _C_WARN=$'\033[1;33m' _C_ERR=$'\033[1;31m' _C_OK=$'\033[1;32m'
	_C_OFF=$'\033[0m'
else
	_C_INFO='' _C_WARN='' _C_ERR='' _C_OK='' _C_OFF=''
fi

info() { printf '%s==>%s %s\n' "$_C_INFO" "$_C_OFF" "$*" >&2; }
ok() { printf '%s✔%s %s\n' "$_C_OK" "$_C_OFF" "$*" >&2; }
warn() { printf '%s⚠%s  %s\n' "$_C_WARN" "$_C_OFF" "$*" >&2; }
die() {
	printf '%s✘%s %s\n' "$_C_ERR" "$_C_OFF" "$*" >&2
	exit 1
}

# env_get FILE KEY -> prints the value without the surrounding quotes ('' if unset).
env_get() {
	local file=$1 key=$2 line value
	[[ -f $file ]] || return 0
	line=$(grep -E "^${key}=" "$file" | tail -n 1) || true
	value=${line#*=}
	if [[ $value == \'*\' && ${#value} -ge 2 ]]; then
		value=${value:1:-1}
	elif [[ $value == \"*\" && ${#value} -ge 2 ]]; then
		value=${value:1:-1}
	fi
	printf '%s' "$value"
}

# env_set FILE KEY VALUE -> writes KEY='VALUE' (replacing an existing line), mode 600.
# Single quotes keep Docker Compose from expanding "$" inside secrets.
env_set() {
	local file=$1 key=$2 value=$3 tmp
	if [[ $value == *"'"* || $value == *$'\n'* ]]; then
		die "El valor de $key no puede contener comillas simples ni saltos de línea"
	fi
	tmp=$(umask 077 && mktemp "${file}.XXXXXX")
	if [[ -f $file ]] && grep -qE "^${key}=" "$file"; then
		# Replace the first occurrence in place (keeps comments and order), drop duplicates.
		# The line goes through ENVIRON because awk -v would interpret backslashes.
		ENV_SET_LINE="${key}='${value}'" awk -v k="$key" '
			$0 ~ "^" k "=" { if (!done) { print ENVIRON["ENV_SET_LINE"]; done = 1 }; next }
			{ print }
		' "$file" >"$tmp"
	elif [[ -f $file ]]; then
		cat "$file" >"$tmp"
		printf "%s='%s'\n" "$key" "$value" >>"$tmp"
	else
		printf "%s='%s'\n" "$key" "$value" >"$tmp"
	fi
	if [[ -f $file ]]; then
		chown --reference="$file" "$tmp" 2>/dev/null || true
	fi
	chmod 600 "$tmp"
	mv "$tmp" "$file"
}

# env_unset FILE KEY -> removes the key if present.
env_unset() {
	local file=$1 key=$2 tmp
	[[ -f $file ]] && grep -qE "^${key}=" "$file" || return 0
	tmp=$(umask 077 && mktemp "${file}.XXXXXX")
	grep -vE "^${key}=" "$file" >"$tmp" || true
	chmod --reference="$file" "$tmp" 2>/dev/null || true
	chown --reference="$file" "$tmp" 2>/dev/null || true
	mv "$tmp" "$file"
}

# curl_secret URL [curl args...] -> like curl, but the URL (which contains a token) is
# passed through a config file on stdin so it never shows up in the process list.
curl_secret() {
	local url=$1
	shift
	printf 'url = "%s"\n' "$url" | curl -K - "$@"
}

# public_ipv4 -> the server's public IPv4 address, as seen from the Internet.
public_ipv4() {
	local ip service
	for service in https://api.ipify.org https://ifconfig.me/ip https://icanhazip.com; do
		ip=$(curl -4 -fsS --max-time 10 "$service" 2>/dev/null | tr -d '[:space:]') || continue
		if [[ $ip =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
			printf '%s' "$ip"
			return 0
		fi
	done
	return 1
}

# image_is_public IMAGE TAG -> succeeds if the GHCR image can be pulled anonymously.
image_is_public() {
	local repo=${1#ghcr.io/} tag=$2 token code
	token=$(curl -fsS --max-time 15 \
		"https://ghcr.io/token?scope=repository:${repo}:pull&service=ghcr.io" 2>/dev/null |
		sed -n 's/.*"token":"\([^"]*\)".*/\1/p') || return 1
	[[ -n $token ]] || return 1
	code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
		-H "Authorization: Bearer ${token}" \
		-H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json' \
		"https://ghcr.io/v2/${repo}/manifests/${tag}") || return 1
	[[ $code == 200 ]]
}

# uses_local_build -> true when .env selects docker-compose.build.yml via COMPOSE_FILE.
uses_local_build() {
	[[ $(env_get "$ROOT/.env" COMPOSE_FILE) == *docker-compose.build.yml* ]]
}

# shellcheck disable=SC2034 # used by the scripts that source this file
BOT_IMAGE_DEFAULT=ghcr.io/jpchaconr/telebot-photos
# shellcheck disable=SC2034
LOCAL_BUILD_COMPOSE_FILE=docker-compose.yml:docker-compose.build.yml
