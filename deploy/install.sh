#!/usr/bin/env bash
# telebot-photos installer for an Ubuntu or Debian VPS (amd64 or arm64).
#
#   curl -fsSL https://raw.githubusercontent.com/jpchaconr/telebot-photos/HEAD/deploy/install.sh | sudo bash
#
# Safe to run again at any time, e.g. to add the Google Drive or OneDrive credentials:
# it keeps the existing configuration and never changes ENCRYPTION_KEY.
#
# Optional environment variables:
#   TELEBOT_DIR=/opt/telebot-photos   install directory
#   TELEBOT_REF=<branch or tag>       repository version to clone (default: the default branch)
#   TELEBOT_NONINTERACTIVE=1          no questions: values come from environment variables
#                                     named like the .env keys (TELEGRAM_BOT_TOKEN, ...)
#   TELEBOT_DOMAIN_MODE=duckdns|sslip|custom, DUCKDNS_SUBDOMAIN=<name>
#   TELEBOT_DEPLOY_USER=1|0           create (or not) the deploy user for GitHub Actions
#   TELEBOT_LOCAL_BUILD=1             build the bot image here instead of pulling it
#   TELEBOT_SKIP_SYSTEM=1             skip packages, Docker, swap, firewall and cron
#   TELEBOT_SKIP_FIREWALL=1, TELEBOT_SKIP_TELEGRAM=1, TELEBOT_SKIP_DNS_CHECK=1
set -euo pipefail

REPO_URL=${TELEBOT_REPO:-https://github.com/jpchaconr/telebot-photos.git}
NONINTERACTIVE=${TELEBOT_NONINTERACTIVE:-0}
SKIP_SYSTEM=${TELEBOT_SKIP_SYSTEM:-0}
SKIP_FIREWALL=${TELEBOT_SKIP_FIREWALL:-0}
SKIP_TELEGRAM=${TELEBOT_SKIP_TELEGRAM:-0}
SKIP_DNS_CHECK=${TELEBOT_SKIP_DNS_CHECK:-0}
VOLUME_BOT_DATA=telebot-photos_bot-data
PUBLIC_IP=''
BOT_USERNAME=''

# --- bootstrap ---------------------------------------------------------------------------
# Piped through curl there is no checkout yet: clone the repository and run its own copy
# of this script, so the helpers in deploy/lib.sh and the compose files are available.

bootstrap() {
	local self=${BASH_SOURCE[0]:-} dir
	if [[ -n $self && -f $self ]]; then
		dir=$(cd "$(dirname "$(readlink -f "$self")")/.." && pwd)
		if [[ -f $dir/docker-compose.yml && -f $dir/deploy/lib.sh ]]; then
			ROOT=$dir
			return 0
		fi
	fi
	if [[ $EUID -ne 0 ]]; then
		echo "Ejecuta el instalador como root: curl -fsSL ... | sudo bash" >&2
		exit 1
	fi
	ROOT=${TELEBOT_DIR:-/opt/telebot-photos}
	if ! command -v git >/dev/null 2>&1; then
		echo "==> Instalando git…" >&2
		apt-get update -qq
		DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git ca-certificates curl >/dev/null
	fi
	if [[ -d $ROOT/.git ]]; then
		# The checkout may belong to the deploy user: tell git that is expected.
		git -c safe.directory="$ROOT" -C "$ROOT" pull --ff-only --quiet ||
			echo "Aviso: no se pudo actualizar $ROOT" >&2
	else
		local clone=(git clone --quiet)
		[[ -n ${TELEBOT_REF:-} ]] && clone+=(--branch "$TELEBOT_REF")
		echo "==> Descargando telebot-photos en $ROOT…" >&2
		"${clone[@]}" "$REPO_URL" "$ROOT"
	fi
	# stdin is the rest of the piped script: questions are read from /dev/tty instead.
	exec bash "$ROOT/deploy/install.sh" "$@" </dev/null
}

# --- questions ---------------------------------------------------------------------------

prompt() { # prompt QUESTION [DEFAULT] [SECRET=0|1] -> answer on stdout
	local question=$1 default=${2:-} secret=${3:-0} answer=''
	[[ -n $default ]] && question="$question [$default]"
	if ((secret)); then
		read -r -s -p "$question: " answer </dev/tty
		printf '\n' >/dev/tty
	else
		read -r -p "$question: " answer </dev/tty
	fi
	answer=${answer#"${answer%%[![:space:]]*}"}
	answer=${answer%"${answer##*[![:space:]]}"}
	printf '%s' "${answer:-$default}"
}

confirm() { # confirm QUESTION -> yes? (default no; always no without a terminal)
	[[ $NONINTERACTIVE == 1 ]] && return 1
	local answer
	answer=$(prompt "$1 [s/N]")
	[[ $answer =~ ^[sSyY] ]]
}

env_has() { grep -qE "^$1=" "$ENV_FILE" 2>/dev/null; }

# ask_value KEY QUESTION REGEX [SECRET] [DEFAULT]: store KEY in .env unless it has a value.
# The value can also come from an environment variable called KEY.
ask_value() {
	local key=$1 question=$2 regex=$3 secret=${4:-0} default=${5:-} value
	[[ -n $(env_get "$ENV_FILE" "$key") ]] && return 0
	value=${!key:-}
	while true; do
		if [[ -z $value ]]; then
			if [[ $NONINTERACTIVE == 1 ]]; then
				[[ -n $default ]] || die "Falta $key: defínela como variable de entorno"
				value=$default
			else
				value=$(prompt "$question" "$default" "$secret")
			fi
		fi
		[[ $value =~ $regex ]] && break
		[[ $NONINTERACTIVE == 1 ]] && die "Valor no válido para $key"
		warn "Valor no válido, prueba otra vez."
		value=''
	done
	env_set "$ENV_FILE" "$key" "$value"
}

# --- system ------------------------------------------------------------------------------

check_platform() {
	[[ -r /etc/os-release ]] || die "No encuentro /etc/os-release: usa Ubuntu o Debian"
	# shellcheck disable=SC1091
	. /etc/os-release
	OS_ID=${ID:-}
	case $OS_ID in
	ubuntu) OS_CODENAME=${UBUNTU_CODENAME:-${VERSION_CODENAME:-}} ;;
	debian) OS_CODENAME=${VERSION_CODENAME:-} ;;
	*) die "Sistema no soportado: ${PRETTY_NAME:-desconocido} (usa Ubuntu o Debian)" ;;
	esac
	case "$OS_ID:${VERSION_ID:-}" in
	ubuntu:22.04 | ubuntu:24.04 | ubuntu:26.04 | debian:12 | debian:13) ;;
	*) warn "No probado en ${PRETTY_NAME:-este sistema}; continúo de todos modos" ;;
	esac
	ARCH=$(dpkg --print-architecture)
	case $ARCH in
	amd64 | arm64) ;;
	*) die "Arquitectura no soportada: $ARCH (se necesita amd64 o arm64)" ;;
	esac
}

apt_install() {
	DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "$@" >/dev/null
}

install_docker() {
	if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
		ok "Docker ya está instalado ($(docker --version | cut -d, -f1))"
		systemctl enable --now docker >/dev/null 2>&1 || true
		return 0
	fi
	info "Instalando Docker Engine desde el repositorio oficial de Docker…"
	local pkg
	for pkg in docker.io docker-doc docker-compose docker-compose-v2 podman-docker containerd runc; do
		if dpkg -s "$pkg" >/dev/null 2>&1; then
			apt-get remove -y -qq "$pkg" >/dev/null
		fi
	done
	install -m 0755 -d /etc/apt/keyrings
	curl -fsSL "https://download.docker.com/linux/${OS_ID}/gpg" -o /etc/apt/keyrings/docker.asc
	chmod a+r /etc/apt/keyrings/docker.asc
	cat >/etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/${OS_ID}
Suites: ${OS_CODENAME}
Components: stable
Signed-By: /etc/apt/keyrings/docker.asc
EOF
	apt-get update -qq
	apt_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
	systemctl enable --now docker >/dev/null
	ok "Docker instalado"
}

configure_docker_logs() {
	[[ -e /etc/docker/daemon.json ]] && return 0
	mkdir -p /etc/docker
	cat >/etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
EOF
	systemctl restart docker
}

configure_swap() {
	local mem_mb
	mem_mb=$(awk '/^MemTotal:/ { print int($2 / 1024) }' /proc/meminfo)
	if ((mem_mb >= 2000)) || [[ -n $(swapon --noheadings --show 2>/dev/null) ]]; then
		return 0
	fi
	info "Creando 2 GB de swap (el servidor solo tiene ${mem_mb} MB de RAM)…"
	if [[ ! -e /swapfile ]]; then
		fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
		chmod 600 /swapfile
		mkswap /swapfile >/dev/null
	fi
	swapon /swapfile
	grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >>/etc/fstab
	echo 'vm.swappiness=10' >/etc/sysctl.d/99-telebot-photos.conf
	sysctl -q -p /etc/sysctl.d/99-telebot-photos.conf || true
}

configure_unattended_upgrades() {
	apt_install unattended-upgrades
	cat >/etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
}

install_system() {
	info "Instalando paquetes base…"
	apt-get update -qq
	apt_install ca-certificates curl git openssl cron gnupg
	install_docker
	configure_docker_logs
	configure_swap
	configure_unattended_upgrades
}

sshd_port() {
	local port
	port=$(sshd -T 2>/dev/null | awk '$1 == "port" { print $2; exit }') || true
	printf '%s' "${port:-22}"
}

is_oracle_cloud() {
	curl -fs --max-time 2 -H 'Authorization: Bearer Oracle' \
		http://169.254.169.254/opc/v2/instance/ >/dev/null 2>&1
}

# Oracle's Ubuntu images reject everything but SSH in iptables, and Oracle warns against
# enabling ufw there. Open 80/443 in both the saved rules and the live ones. Never run
# iptables-restore: it would flush the chains Docker created.
open_ports_oracle() {
	local rules=/etc/iptables/rules.v4 proto port position
	for rule in tcp:80 tcp:443 udp:443; do
		proto=${rule%:*}
		port=${rule#*:}
		if [[ -f $rules ]] && ! grep -qE -- "-p $proto .*--dport $port -j ACCEPT" "$rules"; then
			sed -i "0,/^-A INPUT -j REJECT/s//-A INPUT -p $proto -m state --state NEW -m $proto --dport $port -j ACCEPT\n&/" "$rules"
		fi
		if ! iptables -C INPUT -p "$proto" -m state --state NEW -m "$proto" --dport "$port" -j ACCEPT 2>/dev/null; then
			position=$(iptables -L INPUT --line-numbers -n | awk '$2 == "REJECT" { print $1; exit }')
			iptables -I INPUT "${position:-1}" -p "$proto" -m state --state NEW -m "$proto" --dport "$port" -j ACCEPT
		fi
	done
	ok "Puertos 80/443 abiertos en iptables (Oracle Cloud)"
	warn "En Oracle abre también 80/tcp, 443/tcp y 443/udp en la Security List de la VCN."
}

setup_firewall() {
	if is_oracle_cloud; then
		open_ports_oracle
		return 0
	fi
	if systemctl is-active --quiet firewalld 2>/dev/null; then
		firewall-cmd --quiet --permanent --add-service=http --add-service=https --add-port=443/udp
		firewall-cmd --quiet --reload
		ok "Puertos 80/443 abiertos en firewalld"
		return 0
	fi
	command -v ufw >/dev/null 2>&1 || apt_install ufw
	local port
	port=$(sshd_port)
	# SSH first, so enabling the firewall never cuts the current session.
	ufw allow "${port}/tcp" >/dev/null
	ufw allow 80/tcp >/dev/null
	ufw allow 443/tcp >/dev/null
	ufw allow 443/udp >/dev/null
	if ! ufw status | grep -q '^Status: active'; then
		ufw default deny incoming >/dev/null
		ufw default allow outgoing >/dev/null
		ufw --force enable >/dev/null
	fi
	ok "Cortafuegos (ufw): abiertos SSH (${port}), 80 y 443"
}

# --- configuration -----------------------------------------------------------------------

validate_token() { # validate_token TOKEN -> sets BOT_USERNAME; fails if Telegram rejects it
	local token=$1 response
	BOT_USERNAME=''
	# After logOut the cloud Bot API must not be called again (it would log the bot back in).
	if [[ $SKIP_TELEGRAM == 1 || -e $STATE_DIR/logout-${token%%:*} ]]; then
		return 0
	fi
	response=$(curl_secret "https://api.telegram.org/bot${token}/getMe" -sS --max-time 20) ||
		die "No se pudo contactar con api.telegram.org"
	[[ $response == *'"ok":true'* ]] || return 1
	BOT_USERNAME=$(sed -n 's/.*"username":"\([^"]*\)".*/\1/p' <<<"$response")
}

configure_telegram() {
	local token
	token=$(env_get "$ENV_FILE" TELEGRAM_BOT_TOKEN)
	if [[ -z $token ]]; then
		[[ $NONINTERACTIVE == 1 ]] || info "Token del bot: créalo con @BotFather en Telegram (/newbot)."
		token=${TELEGRAM_BOT_TOKEN:-}
		while true; do
			if [[ -z $token ]]; then
				[[ $NONINTERACTIVE == 1 ]] && die "Falta TELEGRAM_BOT_TOKEN"
				token=$(prompt "Token del bot" "" 1)
			fi
			if [[ $token =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]] && validate_token "$token"; then
				break
			fi
			[[ $NONINTERACTIVE == 1 ]] && die "Telegram no acepta TELEGRAM_BOT_TOKEN"
			warn "Telegram no acepta ese token. Cópialo otra vez desde @BotFather."
			token=''
		done
		env_set "$ENV_FILE" TELEGRAM_BOT_TOKEN "$token"
	else
		validate_token "$token" || die "Telegram ya no acepta TELEGRAM_BOT_TOKEN de $ENV_FILE"
	fi
	[[ -n $BOT_USERNAME ]] && ok "Bot: @$BOT_USERNAME"

	if [[ -z $(env_get "$ENV_FILE" TELEGRAM_API_ID) && $NONINTERACTIVE != 1 ]]; then
		info "api_id y api_hash: https://my.telegram.org → API development tools."
	fi
	ask_value TELEGRAM_API_ID "api_id" '^[0-9]{3,12}$'
	ask_value TELEGRAM_API_HASH "api_hash" '^[0-9a-f]{32}$' 1
}

configure_domain() {
	local domain mode name
	domain=$(env_get "$ENV_FILE" DOMAIN)
	if [[ -z $domain ]]; then
		domain=${DOMAIN:-}
		mode=${TELEBOT_DOMAIN_MODE:-}
		if [[ -z $domain && -z $mode ]]; then
			if [[ $NONINTERACTIVE == 1 ]]; then
				mode=duckdns
			else
				cat >/dev/tty <<'EOF'

¿Qué nombre público tendrá el servidor? (hace falta para el HTTPS y para Google/OneDrive)
  1) DuckDNS, gratis: NOMBRE.duckdns.org  ← recomendado. Crea antes el subdominio en
     https://www.duckdns.org (entras con Google o GitHub) y copia tu token.
  2) sslip.io, gratis y sin registro: el nombre sale de la IP (peor para Google Drive).
  3) Un dominio propio que ya apunte a la IP de este servidor.
EOF
				case $(prompt "Elige 1, 2 o 3" 1) in
				1) mode=duckdns ;;
				2) mode=sslip ;;
				3) mode=custom ;;
				*) die "Opción no válida" ;;
				esac
			fi
		fi
		if [[ -z $domain ]]; then
			case $mode in
			duckdns)
				name=${DUCKDNS_SUBDOMAIN:-}
				while true; do
					if [[ -z $name ]]; then
						[[ $NONINTERACTIVE == 1 ]] && die "Falta DUCKDNS_SUBDOMAIN"
						name=$(prompt "Subdominio de DuckDNS (la parte antes de .duckdns.org)")
					fi
					name=${name,,}
					name=${name%.duckdns.org}
					[[ $name =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] && break
					[[ $NONINTERACTIVE == 1 ]] && die "DUCKDNS_SUBDOMAIN no es válido"
					warn "Solo letras, números y guiones."
					name=''
				done
				domain="${name}.duckdns.org"
				;;
			sslip)
				PUBLIC_IP=${PUBLIC_IP:-$(public_ipv4)} || die "No pude averiguar la IP pública"
				domain="${PUBLIC_IP//./-}.sslip.io"
				;;
			custom)
				[[ $NONINTERACTIVE == 1 ]] && die "Falta DOMAIN"
				domain=$(prompt "Dominio (por ejemplo bot.midominio.com)")
				;;
			*) die "TELEBOT_DOMAIN_MODE debe ser duckdns, sslip o custom" ;;
			esac
		fi
		domain=${domain,,}
		[[ $domain =~ ^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$ ]] || die "Dominio no válido: $domain"
		env_set "$ENV_FILE" DOMAIN "$domain"
	fi
	if [[ $domain == *.duckdns.org ]]; then
		ask_value DUCKDNS_TOKEN "Token de DuckDNS (aparece arriba en duckdns.org)" \
			'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' 1
	fi
	ask_value ACME_EMAIL "Email para Let's Encrypt (avisos de caducidad del certificado)" \
		'^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$'
	if ! env_has CONTACT_EMAIL; then
		local contact=${CONTACT_EMAIL:-}
		if [[ -z $contact && $NONINTERACTIVE != 1 ]]; then
			contact=$(prompt "Email de contacto para la página de privacidad (Enter = ninguno)")
		fi
		env_set "$ENV_FILE" CONTACT_EMAIL "$contact"
	fi
}

configure_misc() {
	local tz
	tz=$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || true)
	ask_value TIMEZONE "Zona horaria para nombrar los archivos" '^([A-Za-z_]+(/[A-Za-z0-9_+-]+)+|UTC)$' \
		0 "${tz:-UTC}"
	env_has BOT_TAG || env_set "$ENV_FILE" BOT_TAG latest

	if [[ -z $(env_get "$ENV_FILE" ENCRYPTION_KEY) ]]; then
		if docker volume inspect "$VOLUME_BOT_DATA" >/dev/null 2>&1; then
			die "Ya hay datos del bot (volumen $VOLUME_BOT_DATA) pero falta ENCRYPTION_KEY en .env.
   Restaura la clave original: sin ella no se pueden leer las configuraciones guardadas.
   Si de verdad quieres empezar de cero: docker volume rm $VOLUME_BOT_DATA"
		fi
		env_set "$ENV_FILE" ENCRYPTION_KEY "$(openssl rand -base64 32 | tr '+/' '-_')"
		ok "Clave de cifrado generada en $ENV_FILE"
	fi
}

# --- DNS, Telegram and image ---------------------------------------------------------------

update_duckdns() {
	[[ $(env_get "$ENV_FILE" DOMAIN) == *.duckdns.org ]] || return 0
	info "Apuntando $(env_get "$ENV_FILE" DOMAIN) a este servidor en DuckDNS…"
	"$ROOT/deploy/duckdns-update.sh"
	ok "DuckDNS actualizado"
}

resolve_ipv4() { # resolve_ipv4 NAME -> first A record, asked to Google's DNS (no local cache)
	local answer
	answer=$(curl -fsS --max-time 10 "https://dns.google/resolve?name=$1&type=A" 2>/dev/null |
		grep -oE '"data":"[0-9.]+"' | head -n 1 | cut -d'"' -f4) || true
	[[ -n $answer ]] || answer=$(getent ahostsv4 "$1" 2>/dev/null | awk '{ print $1; exit }') || true
	printf '%s' "$answer"
}

wait_for_dns() {
	[[ $SKIP_DNS_CHECK == 1 ]] && return 0
	local domain resolved=''
	domain=$(env_get "$ENV_FILE" DOMAIN)
	PUBLIC_IP=${PUBLIC_IP:-$(public_ipv4 || true)}
	if [[ -z $PUBLIC_IP ]]; then
		warn "No pude averiguar la IP pública: no compruebo el DNS"
		return 0
	fi
	info "Comprobando que $domain apunta a $PUBLIC_IP…"
	for _ in $(seq 1 30); do
		resolved=$(resolve_ipv4 "$domain")
		if [[ $resolved == "$PUBLIC_IP" ]]; then
			ok "$domain → $PUBLIC_IP"
			return 0
		fi
		sleep 10
	done
	warn "$domain resuelve a '${resolved:-nada}', no a $PUBLIC_IP. Let's Encrypt no podrá emitir el certificado."
	confirm "¿Continuar de todos modos?" || die "Corrige el DNS y vuelve a ejecutar el instalador"
}

telegram_logout() {
	[[ $SKIP_TELEGRAM == 1 ]] && return 0
	local token marker response
	token=$(env_get "$ENV_FILE" TELEGRAM_BOT_TOKEN)
	marker="$STATE_DIR/logout-${token%%:*}"
	[[ -e $marker ]] && return 0
	info "Desconectando el bot de la API en la nube de Telegram (solo esta vez)…"
	response=$(curl_secret "https://api.telegram.org/bot${token}/logOut" -sS --max-time 20) ||
		die "No se pudo contactar con api.telegram.org"
	if [[ $response == *'"ok":true'* || $response == *'ogged out'* ]]; then
		date -u >"$marker"
		ok "Bot desconectado de la API en la nube: ahora lo atiende el servidor local"
	else
		die "Telegram rechazó logOut: $response"
	fi
}

choose_image() {
	local tag
	tag=$(env_get "$ENV_FILE" BOT_TAG)
	tag=${tag:-latest}
	if [[ ${TELEBOT_LOCAL_BUILD:-0} == 1 ]]; then
		env_set "$ENV_FILE" COMPOSE_FILE "$LOCAL_BUILD_COMPOSE_FILE"
	fi
	if uses_local_build; then
		info "La imagen del bot se construye en este servidor (docker-compose.build.yml)"
		return 0
	fi
	if image_is_public "$BOT_IMAGE_DEFAULT" "$tag"; then
		ok "Imagen ${BOT_IMAGE_DEFAULT}:${tag} disponible"
		return 0
	fi
	warn "La imagen ${BOT_IMAGE_DEFAULT}:${tag} todavía no está publicada (o no es pública)."
	warn "La construyo aquí. Cuando GitHub Actions la publique, 'deploy/update.sh latest' la usará."
	env_set "$ENV_FILE" COMPOSE_FILE "$LOCAL_BUILD_COMPOSE_FILE"
}

start_stack() {
	cd "$ROOT"
	if uses_local_build; then
		info "Construyendo la imagen del bot (unos minutos la primera vez)…"
		docker compose pull --quiet --ignore-buildable
		docker compose build --pull bot
	else
		info "Descargando las imágenes…"
		docker compose pull --quiet
	fi
	info "Arrancando los servicios…"
	if ! docker compose up -d --remove-orphans --wait --wait-timeout 300; then
		docker compose ps >&2 || true
		docker compose logs --tail 40 >&2 || true
		die "Los servicios no arrancaron bien (mira los logs de arriba)"
	fi
	ok "Servicios en marcha"
}

check_https() {
	[[ $SKIP_DNS_CHECK == 1 ]] && return 0
	local url
	url="https://$(env_get "$ENV_FILE" DOMAIN)/health"
	info "Esperando al certificado HTTPS de $url…"
	for _ in $(seq 1 24); do
		if curl -fsS --max-time 10 "$url" >/dev/null 2>&1; then
			ok "HTTPS funcionando"
			return 0
		fi
		sleep 5
	done
	warn "Todavía no responde $url. Revisa que los puertos 80 y 443 estén abiertos en el"
	warn "cortafuegos del proveedor y mira 'docker compose logs caddy'."
}

# --- optional parts ------------------------------------------------------------------------

ask_provider() { # ask_provider NAME ID_KEY SECRET_KEY ID_REGEX -> 0 if newly configured
	local name=$1 id_key=$2 secret_key=$3 id_regex=$4 id secret
	[[ -n $(env_get "$ENV_FILE" "$id_key") ]] && return 1
	id=${!id_key:-}
	secret=${!secret_key:-}
	if [[ -z $id || -z $secret ]]; then
		[[ $NONINTERACTIVE == 1 ]] && return 1
		confirm "¿Configurar ${name} ahora? (puedes hacerlo más tarde volviendo a ejecutar el instalador)" ||
			return 1
		while [[ ! $id =~ $id_regex ]]; do
			id=$(prompt "Client ID de ${name}")
		done
		while [[ -z $secret ]]; do
			secret=$(prompt "Client secret de ${name}" "" 1)
		done
	fi
	[[ $id =~ $id_regex ]] || die "${id_key} no es válido"
	env_set "$ENV_FILE" "$id_key" "$id"
	env_set "$ENV_FILE" "$secret_key" "$secret"
	ok "${name} configurado"
}

configure_oauth() {
	local domain changed=0
	domain=$(env_get "$ENV_FILE" DOMAIN)
	if [[ $NONINTERACTIVE != 1 ]] &&
		[[ -z $(env_get "$ENV_FILE" GOOGLE_CLIENT_ID) || -z $(env_get "$ENV_FILE" MS_CLIENT_ID) ]]; then
		print_console_values "$domain"
	fi
	ask_provider "Google Drive" GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET \
		'^[0-9]+-[A-Za-z0-9_]+\.apps\.googleusercontent\.com$' && changed=1
	ask_provider "OneDrive" MS_CLIENT_ID MS_CLIENT_SECRET \
		'^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$' && changed=1
	if ((changed)); then
		info "Reiniciando el bot con las nuevas credenciales…"
		docker compose up -d --wait --wait-timeout 180 bot
	fi
}

print_console_values() {
	local domain=$1 authorized=$1
	[[ $domain == *.sslip.io ]] && authorized=sslip.io
	cat >&2 <<EOF

Datos para las consolas de Google y Microsoft (paso a paso en DEPLOY.md):
  Google Auth Platform → Branding
    Página principal:      https://${domain}/
    Política de privacidad: https://${domain}/privacy
    Dominio autorizado:     ${authorized}
  Google → Clients → Web application
    Redirect URI:           https://${domain}/oauth/google/callback
  Microsoft Entra → App registrations → Authentication (Web)
    Redirect URI:           https://${domain}/oauth/microsoft/callback

EOF
}

setup_deploy_user() {
	local want=${TELEBOT_DEPLOY_USER:-} user=deploy home auth tmp port ip hostkey known_host
	if [[ -z $want ]]; then
		confirm "¿Preparar el despliegue automático desde GitHub Actions (usuario 'deploy' con clave SSH)?" ||
			return 0
	elif [[ $want != 1 ]]; then
		return 0
	fi
	id "$user" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash "$user"
	usermod -aG docker "$user"
	# update.sh runs as this user: it must own the checkout (git pull, .env, backups).
	chown -R "$user:$user" "$ROOT"
	home=$(getent passwd "$user" | cut -d: -f6)
	auth="$home/.ssh/authorized_keys"
	install -d -m 700 -o "$user" -g "$user" "$home/.ssh"
	if grep -q 'telebot-photos-ci' "$auth" 2>/dev/null; then
		confirm "Ya existe una clave de despliegue. ¿Crear otra? (la anterior dejará de funcionar)" ||
			return 0
	fi
	tmp=$(mktemp -d)
	ssh-keygen -q -t ed25519 -N '' -C telebot-photos-ci -f "$tmp/key"
	{
		grep -v 'telebot-photos-ci' "$auth" 2>/dev/null || true
		# The key can only run update.sh (the tag travels in SSH_ORIGINAL_COMMAND).
		printf 'restrict,command="%s/deploy/update.sh" %s\n' "$ROOT" "$(cat "$tmp/key.pub")"
	} >"$tmp/authorized_keys"
	install -m 600 -o "$user" -g "$user" "$tmp/authorized_keys" "$auth"

	port=$(sshd_port)
	ip=${PUBLIC_IP:-$(public_ipv4 || hostname -I | awk '{ print $1 }')}
	hostkey=$(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)
	if [[ $port == 22 ]]; then
		known_host="$ip $hostkey"
	else
		known_host="[$ip]:$port $hostkey"
	fi
	cat <<EOF

=====================================================================================
 Secretos para GitHub: Settings → Environments → production → Environment secrets
=====================================================================================
 SSH_HOST         ${ip}
 SSH_PORT         ${port}
 SSH_USER         ${user}
 SSH_KNOWN_HOSTS  ${known_host}
 SSH_PRIVATE_KEY  (todo el bloque siguiente, incluidas las líneas BEGIN y END)

$(cat "$tmp/key")

 Esta clave privada no se guarda en el servidor: cópiala ahora.
=====================================================================================

EOF
	rm -rf "$tmp"
}

install_cron() {
	local owner
	owner=$(stat -c %U "$ROOT")
	{
		echo "# Installed by deploy/install.sh"
		echo "SHELL=/bin/bash"
		echo "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
		echo "17 3 * * * ${owner} ${ROOT}/deploy/backup.sh --quiet >>${ROOT}/.state/cron.log 2>&1"
		if [[ $(env_get "$ENV_FILE" DOMAIN) == *.duckdns.org ]]; then
			echo "7 * * * * ${owner} ${ROOT}/deploy/duckdns-update.sh >>${ROOT}/.state/cron.log 2>&1"
		fi
	} >/etc/cron.d/telebot-photos
	chmod 644 /etc/cron.d/telebot-photos
}

summary() {
	local domain
	domain=$(env_get "$ENV_FILE" DOMAIN)
	cat <<EOF

✔ telebot-photos está en marcha en https://${domain}/

  Siguiente paso: añade ${BOT_USERNAME:+@$BOT_USERNAME }el bot a un grupo y escribe /configurar.
  (S3 funciona ya; Google Drive y OneDrive necesitan sus credenciales: ver DEPLOY.md.)

  Logs:            cd ${ROOT} && docker compose logs -f bot
  Actualizar:      sudo ${ROOT}/deploy/update.sh latest
  Copia de datos:  sudo ${ROOT}/deploy/backup.sh   (también cada noche a las 03:17)

  ⚠ Guarda una copia de ${ENV_FILE} (contiene ENCRYPTION_KEY) fuera del servidor.
    Sin esa clave, las configuraciones guardadas no se pueden recuperar.
EOF
}

main() {
	bootstrap "$@"
	# shellcheck source=deploy/lib.sh
	source "$ROOT/deploy/lib.sh"
	ENV_FILE="$ROOT/.env"
	STATE_DIR="$ROOT/.state"
	[[ $EUID -eq 0 ]] || die "Ejecuta el instalador como root (sudo)"
	if [[ $NONINTERACTIVE != 1 ]] && ! { : </dev/tty; } 2>/dev/null; then
		die "No hay terminal para hacer preguntas: usa TELEBOT_NONINTERACTIVE=1 y variables de entorno"
	fi
	cd "$ROOT"
	mkdir -p "$STATE_DIR" "$ROOT/backups"
	chmod 700 "$STATE_DIR" "$ROOT/backups"
	if [[ ! -e $ENV_FILE ]]; then
		(umask 077 && printf '%s\n' \
			"# Written by deploy/install.sh. See .env.example for every option." \
			"# Keep values in 'single quotes' and never change ENCRYPTION_KEY." >"$ENV_FILE")
	fi
	chmod 600 "$ENV_FILE"

	check_platform
	if [[ $SKIP_SYSTEM != 1 ]]; then
		install_system
		[[ $SKIP_FIREWALL == 1 ]] || setup_firewall
	fi
	command -v docker >/dev/null 2>&1 || die "Docker no está instalado"

	configure_telegram
	configure_domain
	configure_misc
	update_duckdns
	wait_for_dns
	telegram_logout
	choose_image
	start_stack
	check_https
	configure_oauth
	setup_deploy_user
	[[ $SKIP_SYSTEM == 1 ]] || install_cron
	summary
}

main "$@"
