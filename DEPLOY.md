# Desplegar telebot-photos en un VPS

Esta guía instala el bot en un servidor en la nube (VPS) con un **dominio gratuito de DuckDNS** y HTTPS automático. Además deja preparado el **despliegue automático desde GitHub**.

```
                           GitHub
  push / "Run workflow" ─▶ Actions: tests → imagen amd64+arm64 → ghcr.io/jpchaconr/telebot-photos
                                                                   │
                                          ssh (clave limitada)     ▼
  Telegram ◀──▶ telegram-bot-api ◀──▶ bot ◀── Caddy (HTTPS) ◀── https://NOMBRE.duckdns.org
                (solo modo 2 GB)       │                           /, /privacy, /oauth/…
                                       └──▶ Google Drive · OneDrive · S3 (lo elige cada grupo)
```

En el modo de **20 MB** (el recomendado) no hay `telegram-bot-api`: el bot descarga directamente de la API de Telegram. El modo de **2 GB** añade ese servidor local; lo eliges al instalar (ver [apartado 3](#3-crear-el-bot-de-telegram)).

**Resumen**

1. Crear un VPS con Ubuntu 24.04 y abrir los puertos 80 y 443.
2. Crear el subdominio en DuckDNS.
3. Crear el bot en Telegram.
4. Ejecutar el instalador y elegir el tamaño máximo de archivo: 20 MB o 2 GB.
5. (Opcional) Conectar Google Drive y OneDrive.
6. (Opcional) Activar el despliegue automático.

---

## 1. Elegir y preparar el VPS

**Tamaño**

- **Archivos de hasta 20 MB**: 1 vCPU, 1 GB de RAM y 25 GB de disco bastan. El instalador añade 2 GB de swap.
- **Archivos de hasta 2 GB**: funciona con lo mismo, pero mejor 2 vCPU, 2–4 GB de RAM y 40 GB de disco o más si se envían vídeos de 1–2 GB.

Cada archivo pasa entero por el disco antes de subirse. El tráfico mensual será más o menos el doble del volumen de fotos y vídeos: entra desde Telegram y sale hacia la nube.

**Sistema**: Ubuntu 22.04/24.04/26.04 o Debian 12/13, en **amd64 o arm64**. La imagen del bot se publica para las dos arquitecturas.

Opciones orientativas (precios de octubre de 2026; comprueba stock y precio al contratar):

| Proveedor | Plan | Precio/mes | Notas |
|---|---|---|---|
| Oracle Cloud Always Free | VM.Standard.A1.Flex, **arm64**, hasta 2 OCPU / 12 GB | 0 € | Gratis, pero: "out of host capacity" frecuente (prueba otra zona o reintenta), reclama instancias **ociosas** (este bot lo está casi siempre) y hay que abrir puertos en la *Security List*. Recomendado: pasar la cuenta a *Pay As You Go* (lo Always Free sigue gratis) con una alerta de presupuesto. |
| Hetzner | CX23 (x86) o CAX11 (ARM), 2 vCPU / 4 GB | ~6 € con IPv4 | La mejor relación calidad/precio **cuando hay stock**: en octubre de 2026 aparecen a menudo como no disponibles y Hetzner limita altas nuevas. Alternativa: CPX02 (1 vCPU / 1 GB). |
| Vultr | 1 vCPU / 1 GB (o 2 GB) | 5 $ (10 $) | IPv4 incluida. |
| DigitalOcean | Basic 1 GB (o 2 GB) | 6 $ (12 $) | IPv4 incluida. |
| AWS Lightsail | 1 GB (o 2 GB) | 7 $ (12 $) | Cuenta el tráfico de entrada y de salida. Hay que abrir 443 en su cortafuegos. |
| Contabo | Cloud VPS, 4 vCPU / 8 GB | ~5 € | Mucha RAM por euro, CPU compartida. Revisa si cobra alta en el pago mensual. |

> No uses la e2-micro gratuita de Google Cloud: tiene 1 GB, solo está en EE. UU., cobra la IPv4 y casi no incluye tráfico de salida, que es justo lo que gasta el bot al subir archivos.

**Antes de seguir:**

- Crea el VPS con tu **clave SSH** y anota su **IP pública**.
- **Abre los puertos TCP 80, TCP 443 y UDP 443** en el cortafuegos del proveedor, si tiene uno. En Oracle hay que hacerlo siempre (*VCN → Security List → Ingress Rules*, origen `0.0.0.0/0`). En Hetzner, DigitalOcean y Vultr solo si le asignas un cortafuegos. El cortafuegos del propio servidor lo configura el instalador.

## 2. Dominio gratuito con DuckDNS

1. Entra en <https://www.duckdns.org> con Google, GitHub u otra cuenta.
2. Escribe un subdominio, por ejemplo `fotosfamilia`, y pulsa **add domain**. Tu dominio será `fotosfamilia.duckdns.org`.
3. Copia el **token** que aparece arriba. No hace falta poner la IP: el instalador la actualiza, y un cron la revisa cada hora.

**¿Por qué DuckDNS y no sslip.io?**

- `duckdns.org` está en la *Public Suffix List*, así que Google trata `fotosfamilia.duckdns.org` como tu propio dominio (puedes incluso verificarlo).
- Let's Encrypt le da un cupo de certificados propio.

**sslip.io** también funciona y no necesita registro (`203-0-113-10.sslip.io`; el instalador lo ofrece como opción 2), pero tiene inconvenientes:

- En Google hay que declarar `sslip.io` como dominio autorizado, un dominio compartido que nadie puede verificar.
- Su cupo de Let's Encrypt es compartido y se agotó varias veces en 2026.
- Si cambia la IP del servidor, cambia el nombre.

Si más adelante compras un dominio, el cambio es sencillo: ver *Pasar a un dominio propio* en el apartado 8.

## 3. Crear el bot de Telegram

1. En [@BotFather](https://t.me/BotFather): `/newbot` y guarda el **token**.
2. **Desactiva el privacy mode**: `/setprivacy` → tu bot → **Disable**. Si no, el bot no ve las fotos de los grupos.
3. **Solo para archivos de hasta 2 GB**: en <https://my.telegram.org> → *API development tools*, crea una aplicación y anota el **api_id** y el **api_hash**. Los usa el servidor local de la Bot API.

**¿Archivos de hasta 20 MB o de hasta 2 GB?** El instalador te lo pregunta:

| | Hasta 20 MB (recomendado) | Hasta 2 GB |
|---|---|---|
| Cómo descarga | API de Telegram | Servidor local de la Bot API (un contenedor más) |
| Qué necesitas | Solo el token | El token, el `api_id` y el `api_hash` |
| VPS | 1 GB de RAM y 25 GB de disco | Mejor 2 GB de RAM y 40 GB de disco |
| Qué no se guarda | Vídeos largos (a partir de 1–3 minutos, según la calidad), vídeos enviados "como archivo" y documentos grandes. El bot avisa en el grupo | Nada por debajo de 2 GB |

Las fotos, las notas de voz, los GIF y los vídeos cortos se guardan igual en los dos. Puedes cambiar de modo más adelante (apartado 8, *Cambiar el tamaño máximo*).

## 4. Instalar

Conéctate al VPS por SSH y ejecuta:

```bash
curl -fsSL https://raw.githubusercontent.com/jpchaconr/telebot-photos/HEAD/deploy/install.sh | sudo bash
```

El instalador:

1. Instala Docker (desde el repositorio oficial), unattended-upgrades y, si hay menos de 2 GB de RAM, swap.
2. Configura el cortafuegos: `ufw` con SSH, 80 y 443. En Oracle usa reglas de iptables, porque allí no se debe activar ufw.
3. Descarga el proyecto en `/opt/telebot-photos`.
4. Te pide:
   - el token del bot (lo comprueba);
   - el tamaño máximo de archivo: **1 (20 MB)** o **2 (2 GB)**; con 2 GB, también el `api_id` y el `api_hash`;
   - el dominio: elige **1 (DuckDNS)**, escribe el subdominio y el token;
   - un email para Let's Encrypt y, si quieres, un email de contacto para la página de privacidad;
   - la zona horaria.
5. Genera `ENCRYPTION_KEY` y escribe `/opt/telebot-photos/.env` con permisos `600`.
6. Apunta DuckDNS a la IP del VPS y espera a que el DNS responda.
7. Solo con 2 GB: desconecta el bot de la API en la nube de Telegram (`logOut`, una sola vez), porque lo va a atender el servidor local.
8. Descarga la imagen del bot y arranca los servicios. Si la imagen aún no está publicada en GHCR, la construye en el propio servidor.
9. Espera al certificado HTTPS y te muestra los datos para las consolas de Google y Microsoft.
10. Te ofrece conectar Google Drive y OneDrive (puedes hacerlo más tarde) y preparar el despliegue automático.
11. Programa una copia de seguridad diaria a las 03:17.

**Comprueba que funciona:**

- `https://TU_DOMINIO/health` responde `{"status": "ok", ...}`.
- `https://TU_DOMINIO/` muestra la página del bot.
- Añade el bot a un grupo y escribe `/configurar`. Con **S3** ya funciona todo; Google Drive y OneDrive necesitan los pasos 5 y 6.

> ⚠️ **Guarda una copia de `/opt/telebot-photos/.env` fuera del servidor** (en un gestor de contraseñas, por ejemplo). Contiene `ENCRYPTION_KEY`: sin ella no se pueden leer las configuraciones guardadas. El instalador **nunca** la regenera.

Puedes volver a ejecutar el instalador cuando quieras, con el mismo comando o con `sudo bash /opt/telebot-photos/deploy/install.sh`. Conserva lo que ya está configurado y solo pregunta lo que falta.

## 5. Conectar Google Drive (opcional)

Hazlo con el bot ya instalado, porque Google comprueba la página principal y la de privacidad que sirve el propio bot. El instalador te mostró estos valores; también los ves si lo vuelves a ejecutar.

1. En <https://console.cloud.google.com/> crea un proyecto.
2. Activa la API: *APIs & Services → Library* → **Google Drive API** → *Enable*.
3. Abre **Google Auth Platform**. Configúralo **en este orden**:
   1. **Branding**
      - Nombre de la app y email de soporte.
      - *Application home page*: `https://TU_DOMINIO/`
      - *Privacy policy*: `https://TU_DOMINIO/privacy`
      - *Authorized domains*: `TU_DOMINIO` (por ejemplo `fotosfamilia.duckdns.org`, **nunca** solo `duckdns.org`; con sslip.io sería `sslip.io`).
      - **No subas logo**: si lo subes, Google pide verificar la marca.
   2. **Data Access** → *Add or remove scopes* → marca solo `.../auth/drive.file`. Es un permiso no sensible: el bot solo ve lo que él mismo crea.
   3. **Audience** → tipo **External** → **Publish app** (estado *In production*). Imprescindible: en *Testing*, los permisos caducan a los 7 días y el bot dejaría de subir a Drive cada semana.
   4. **Clients** → *Create client* → tipo **Web application**.
      - *Authorized redirect URIs*: `https://TU_DOMINIO/oauth/google/callback`
      - **Descarga o copia el Client secret ahora**: solo se muestra una vez.
4. Vuelve a ejecutar el instalador y pega el *Client ID* y el *Client secret* cuando pregunte por Google Drive.
5. En un grupo: `/configurar` → Google Drive → *Autorizar*. Autoriza siempre **después** de publicar la app.

**Notas**

- Como la marca no está verificada, Google muestra el dominio en vez del nombre de la app en la pantalla de permiso. Para un bot privado es normal.
- **Opcional**: si quieres verificar el dominio (Google Search Console → propiedad de tipo *Dominio*), DuckDNS permite fijar el registro TXT que te den:
  ```bash
  curl "https://www.duckdns.org/update?domains=SUBDOMINIO&token=TOKEN&txt=google-site-verification=XXXX"
  ```
- Si nadie usa Drive durante 6 meses, Google borra el cliente OAuth sin uso. Se puede restaurar en los 30 días siguientes.

## 6. Conectar OneDrive (opcional)

Necesitas una cuenta de Azure o un directorio de Microsoft Entra; darse de alta en Azure puede pedir verificación por tarjeta o teléfono.

1. En <https://entra.microsoft.com> → **App registrations** → **New registration**.
   - *Supported account types*: **Accounts in any organizational directory and personal Microsoft accounts**.
   - *Redirect URI*: plataforma **Web**, `https://TU_DOMINIO/oauth/microsoft/callback`
2. Copia el **Application (client) ID**.
3. **Certificates & secrets → New client secret**. Elige la caducidad (máximo 24 meses) y copia el **Value**, que solo se muestra una vez.
   - **Apunta en tu calendario cuándo caduca**: ese día dejan de funcionar todas las conexiones de OneDrive.
   - Para renovarlo: crea un secret nuevo, deja vacía la línea `MS_CLIENT_SECRET=''` en `/opt/telebot-photos/.env` (`sudo nano /opt/telebot-photos/.env`) y vuelve a ejecutar el instalador. Solo te pedirá el secret nuevo y reiniciará el bot. Con Google se renueva igual, vaciando `GOOGLE_CLIENT_SECRET`.
4. Vuelve a ejecutar el instalador y pega el ID y el secret cuando pregunte por OneDrive.

Los permisos (`Files.ReadWrite`, `offline_access`, `User.Read`) se piden al autorizar. Las cuentas personales funcionan siempre. Algunas cuentas de empresa necesitan que su administrador apruebe la app.

## 7. Despliegue automático con GitHub Actions (opcional)

El workflow `.github/workflows/deploy.yml`:

1. pasa los tests;
2. construye la imagen para amd64 y arm64;
3. la publica en `ghcr.io/jpchaconr/telebot-photos` con las etiquetas `sha-xxxxxxx` y `latest`;
4. entra por SSH al VPS y ejecuta `deploy/update.sh sha-xxxxxxx`.

**En el VPS**: vuelve a ejecutar el instalador y responde **sí** a "¿Preparar el despliegue automático…?". El instalador:

- crea el usuario `deploy` (en el grupo `docker`);
- genera una clave SSH que **solo puede ejecutar `deploy/update.sh`** (comando forzado en `authorized_keys`);
- **muestra una vez** los cinco secretos para GitHub.

**En GitHub**: *Settings → Environments → New environment* → `production`.

1. En *Environment secrets*, añade los valores que mostró el instalador: `SSH_HOST`, `SSH_PORT`, `SSH_USER`, `SSH_KNOWN_HOSTS` y `SSH_PRIVATE_KEY` (el bloque entero, con las líneas `BEGIN` y `END`).
2. **Opcional**: en *Environment variables*, `PUBLIC_URL` = `https://TU_DOMINIO`. El workflow comprobará que `/health` muestra la nueva versión.
3. Deja *Deployment branches* en **No restriction**, o limítalo a `main` cuando sea la rama predeterminada.

**Lanzarlo**: *Actions → Deploy → Run workflow*.

- La primera vez, comprueba que el paquete es **público**. El workflow avisa si no lo es. También puedes verlo en tu perfil → *Packages* → `telebot-photos`.
- Si no lo es, el despliegue de esa primera ejecución falla (el bot sigue con la versión anterior). Arréglalo y vuelve a lanzarlo:
  - opción recomendada: *Package settings → Change visibility → Public*. Es irreversible;
  - alternativa: iniciar sesión en GHCR **como el usuario que despliega**, con un token *classic* que tenga permiso `read:packages`: `sudo -u deploy -H docker login ghcr.io -u TU_USUARIO_DE_GITHUB`. Si además actualizas a mano con `sudo`, repítelo con `sudo docker login ghcr.io -u TU_USUARIO_DE_GITHUB`.
- Sin los secretos SSH, el workflow solo publica la imagen y lo indica.

**A partir de entonces**

- Los pushes a `main` y las etiquetas `v*` despliegan solos.
- Marca `main` como rama predeterminada en *Settings → General → Default branch*. El servidor sigue siempre la rama predeterminada del repositorio, así que en el siguiente despliegue pasará solo a `main`, y el comando de instalación (`…/HEAD/deploy/install.sh`) también la usará.
- Mientras `main` no sea la predeterminada, despliega a mano con *Run workflow*.

## 8. Operación diaria

Todo se hace desde `/opt/telebot-photos`:

| Qué | Comando |
|---|---|
| Ver logs | `sudo docker compose logs -f bot` (o `caddy`, y `telegram-bot-api` en el modo 2 GB) |
| Estado | `sudo docker compose ps` |
| Actualizar a la última imagen | `sudo deploy/update.sh latest` |
| Volver a una versión anterior | `sudo deploy/update.sh sha-1a2b3c4`. Las etiquetas están en *Packages* o en las ejecuciones de *Actions*. |
| Reiniciar | `sudo docker compose restart bot` (no relee `.env`; tras editarlo usa `sudo docker compose up -d`) |
| Copia de seguridad ahora | `sudo deploy/backup.sh` → `backups/bot-FECHA.db` (se guardan las 14 últimas) |
| Restaurar una copia | `sudo deploy/restore.sh backups/bot-FECHA.db` (con la misma `ENCRYPTION_KEY` con la que se hizo) |
| Espacio en disco | `df -h /` y `sudo docker system df` |

**Más detalles**

- **Actualizaciones**: `update.sh` hace una copia de seguridad, actualiza los ficheros del repositorio, descarga la imagen y reinicia. **Si el bot no arranca, vuelve solo a la imagen que estaba funcionando.** Después borra las imágenes antiguas para no llenar el disco; volver a una versión anterior la descarga otra vez de GHCR.
- **Copias de seguridad**: se hacen con `VACUUM INTO` dentro del contenedor, porque copiar `bot.db` a mano con el bot en marcha puede perder datos. Las copias guardan las credenciales cifradas: guárdalas junto con una copia de `.env` en otro sitio, pero no en el mismo.
- **Si cambia la IP del servidor**:
  - Con DuckDNS se actualiza sola en menos de una hora (o al momento con `sudo deploy/duckdns-update.sh`).
  - Con sslip.io cambia el dominio:
    1. cambia `DOMAIN` en `.env` y aplica el cambio con `sudo docker compose up -d` (`restart` no basta);
    2. actualiza los redirect URIs en Google y Microsoft, y en Google también la página principal, la de privacidad y el dominio autorizado.

    Las conexiones ya hechas siguen funcionando.
- **Mover el bot a otro servidor (o recuperarlo tras perder el VPS)**:
  1. Ejecuta el instalador en el servidor nuevo como siempre. Puedes reutilizar el mismo subdominio de DuckDNS: el instalador lo apunta a la nueva IP.
  2. Pon tu `ENCRYPTION_KEY` guardada en `/opt/telebot-photos/.env` en lugar de la generada (`sudo nano /opt/telebot-photos/.env`, entre comillas simples).
  3. Copia tu copia de seguridad a `/opt/telebot-photos/backups/` y ejecuta `sudo deploy/restore.sh backups/bot-FECHA.db`.

  Sin la clave original, los grupos tendrían que volver a usar `/configurar`.
- **Pasar a un dominio propio** (por ejemplo, si compras `midominio.com`). Todo sale de `DOMAIN` en `.env`: no se migran datos, los grupos conectados siguen funcionando (sus tokens no dependen del dominio) y el corte es de unos segundos.
  1. **DNS**: crea un registro **A** `bot.midominio.com` → IP del VPS. En Cloudflare, déjalo en *DNS only* (nube gris) para que Caddy obtenga el certificado sin problemas.
  2. **Servidor**: cambia el dominio y vuelve a ejecutar el instalador. Comprobará el DNS, reiniciará el bot y Caddy (que pide el certificado nuevo) y quitará el cron de DuckDNS:
     ```bash
     sudo nano /opt/telebot-photos/.env      # DOMAIN='bot.midominio.com'
     sudo bash /opt/telebot-photos/deploy/install.sh
     ```
  3. **Google Auth Platform**:
     - En *Branding*, añade `midominio.com` a *Authorized domains* y cambia la página principal y la de privacidad a `https://bot.midominio.com/` y `https://bot.midominio.com/privacy`.
     - En *Clients*, añade el redirect `https://bot.midominio.com/oauth/google/callback`.
     - Cuando compruebes que `/configurar` funciona, quita lo de DuckDNS.
  4. **Microsoft Entra**: en *Authentication*, **sustituye** el redirect URI por `https://bot.midominio.com/oauth/microsoft/callback`. No lo añadas junto al antiguo: en apps multiinquilino sin editor verificado, Microsoft no admite redirects de dominios raíz distintos.
  5. **GitHub**: si definiste la variable `PUBLIC_URL` en el environment `production`, cámbiala. Los secretos SSH usan la IP y no cambian.

  El subdominio de DuckDNS puedes dejarlo o borrarlo; `DUCKDNS_TOKEN` en `.env` deja de usarse. Con dominio propio también puedes verificarlo en Google Search Console: si algún día pides la verificación de marca, Google podrá mostrar el nombre del bot en la pantalla de permiso.
- **Cambiar el tamaño máximo** (de 20 MB a 2 GB o al revés): cambia `TELEGRAM_BOT_API` en `.env` y vuelve a ejecutar el instalador.
  ```bash
  sudo nano /opt/telebot-photos/.env      # TELEGRAM_BOT_API='local' (2 GB) o 'cloud' (20 MB)
  sudo bash /opt/telebot-photos/deploy/install.sh
  ```
  - **A 2 GB**: te pide el `api_id` y el `api_hash` (apartado 3), hace el `logOut` y arranca el servidor local.
  - **A 20 MB**: quita el servidor local. Telegram no acepta el bot en su API hasta 10 minutos después del `logOut`; si cambias justo después de instalar, el instalador espera lo que falte. Los archivos pendientes se vuelven a descargar de Telegram, y los de más de 20 MB fallan con un aviso en el grupo. Para liberar el espacio del servidor local: `sudo docker volume rm telebot-photos_bot-api-data`.

  Hazlo siempre con el instalador: `update.sh` no cambia de modo, y el servidor local necesita el `logOut` antes de arrancar.
- **Avisos de caducidad del certificado**: Let's Encrypt ya no los envía por email y Caddy renueva solo. Si quieres un aviso cuando algo falle, añade `https://TU_DOMINIO/health` a un monitor gratuito (por ejemplo UptimeRobot).
- **Disco** (modo 2 GB): los archivos de los trabajos que se están reintentando se quedan en el volumen `telebot-photos_bot-api-data` hasta que terminan. Por ejemplo, si un grupo tiene que reconectar su cuenta y sigue enviando vídeos. `/estado` en el grupo muestra los pendientes.
- **Endurecer SSH**: cuando hayas comprobado que entras con tu clave, desactiva las contraseñas. Hazlo en un fichero propio, porque en muchas imágenes cloud `/etc/ssh/sshd_config.d/50-cloud-init.conf` las vuelve a activar:
  ```bash
  printf 'PasswordAuthentication no\nPermitRootLogin prohibit-password\n' | sudo tee /etc/ssh/sshd_config.d/00-hardening.conf
  sudo sshd -T | grep -Ei '^(passwordauthentication|permitrootlogin)'   # comprueba el resultado
  sudo systemctl reload ssh
  ```
- **No borres los volúmenes**: nunca uses `docker compose down -v`. `telebot-photos_bot-data` es la base de datos, y `telebot-photos_caddy-data` guarda los certificados; repetirlos consume cupo de Let's Encrypt.

## 9. Problemas frecuentes

| Síntoma | Causa y solución |
|---|---|
| No sale el certificado HTTPS (`sudo docker compose logs caddy` muestra errores de ACME) | Los puertos 80/443 están cerrados en el cortafuegos del proveedor, o el DNS no apunta a la IP. Comprueba con `curl -I http://TU_DOMINIO` desde fuera. En Oracle, revisa la *Security List*. |
| `too many certificates already issued` | Has pedido demasiados certificados (pasa con sslip.io compartido o si se borró `caddy-data`). Usa DuckDNS o espera unos días. |
| Google: `redirect_uri_mismatch` | El redirect URI del cliente no coincide **exactamente** con `https://TU_DOMINIO/oauth/google/callback`: https, sin barra final, mismo dominio. Los cambios en Google tardan unos minutos. |
| Google: "Invalid domain" al añadir el dominio autorizado | Pon el dominio completo (`fotosfamilia.duckdns.org`). Con sslip.io, si Google lo rechaza, cambia a DuckDNS. |
| Drive deja de funcionar a los 7 días | La app de Google está en *Testing*: publícala (*Audience → Publish app*) y vuelve a usar `/configurar`. |
| Microsoft: `AADSTS50011` | El redirect URI no está registrado como plataforma **Web** o no coincide exactamente. |
| OneDrive deja de funcionar en todos los grupos | Ha caducado el client secret de Microsoft: renuévalo (paso 6). |
| `update.sh`: `denied` o `unauthorized` al descargar la imagen | El paquete de GHCR es privado: hazlo público o inicia sesión como el usuario que despliega (paso 7). |
| El bot no responde en los grupos | El *privacy mode* sigue activado en BotFather (`/setprivacy` → Disable) o el bot no está en el grupo. Mira `sudo docker compose logs bot`. |
| El bot responde "No he guardado este archivo: pesa X MB…" | Estás en el modo de 20 MB y el archivo es más grande. Un vídeo enviado sin marcar "como archivo" se comprime y a veces cabe; si no, cambia a 2 GB (apartado 8, *Cambiar el tamaño máximo*). |
| El log del bot dice "The Bot API is not reachable yet" | En el modo 2 GB: el servidor local de la Bot API está arrancando, o `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` son incorrectos. Revisa `sudo docker compose logs telegram-bot-api`. |
| Oracle: "Out of host capacity" al crear la instancia | No hay hueco en esa zona: prueba otro *availability domain* o vuelve a intentarlo más tarde. |
| DuckDNS no resuelve | DuckDNS ha tenido caídas puntuales. Caddy renueva los certificados con semanas de margen, así que una caída corta no los afecta, pero durante la caída no funcionan las autorizaciones OAuth. |
