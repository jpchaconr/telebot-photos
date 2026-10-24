# telebot-photos

Bot de Telegram que guarda automáticamente **todas las fotos, vídeos, GIFs, audios, notas de voz y documentos** que se envían en un grupo, en el almacenamiento en la nube que elija **cada grupo**:

- **Google Drive**: el administrador autoriza su cuenta con OAuth.
- **OneDrive**: cuentas personales o de empresa, también por OAuth.
- **S3 compatible**: AWS S3, Cloudflare R2, Backblaze B2, MinIO… con endpoint, bucket y claves.

Cada grupo tiene su propia configuración: un grupo puede guardar en el Drive de Ana y otro en un bucket R2 de Luis, con el mismo bot.

## Cómo funciona

```
Grupo ──foto/vídeo──▶ bot ──▶ cola persistente (SQLite)
                                   │
                         workers ◀─┘
                           │ descarga de Telegram (hasta 20 MB, o 2 GB con la Bot API local)
                           │ sube a la nube configurada para ese grupo
                           └─▶ reacciona 👍 al mensaje (o avisa si falla)
```

- **Configuración por chat**: un admin escribe `/configurar` en el grupo y el bot le lleva al chat privado, para que las credenciales nunca se escriban en el grupo.
- **Credenciales cifradas** en la base de datos con `ENCRYPTION_KEY` (Fernet).
- **Cola persistente** con reintentos y backoff exponencial: si el servidor se reinicia o la nube falla un rato, no se pierde nada.
- **Sin duplicados**: el mismo archivo reenviado varias veces al grupo se guarda una sola vez.
- **Galería web de solo lectura**: con `/galeria`, los miembros del grupo ven las fotos, vídeos y GIFs guardados en una web propia, con filtros por persona y por mes.
- **Archivos de hasta 20 MB** con la API de Telegram, o **hasta 2 GB** si añades el servidor local de la Bot API (el instalador lo pregunta). Con 20 MB, el bot avisa en el grupo de los archivos más grandes en vez de guardarlos.

Los archivos se organizan por mes:

```
Telegram - Familia/                       (Drive)   ·  telegram/Familia/ (S3)
└── 2026-10/
    ├── 20261018-153005_ana_1234.jpg
    ├── 20261018-153112_luis_1240.mp4
    └── 20261018-160000_ana_1251_presupuesto.pdf
```

Cada archivo guarda en sus metadatos **quién lo envió** (nombre y @usuario) y **el comentario** (pie de foto), si lo tiene:

- **Drive y OneDrive**: en la descripción del archivo (en OneDrive, solo en cuentas personales):
  ```
  Enviado por: Ana López (@ana)
  Comentario: Cumpleaños de la abuela
  ```
- **S3**: en los metadatos `sender`, `sender-id` (el ID de Telegram, que no cambia aunque cambie el nombre) y `caption`, codificados como URL (`Ana%20L%C3%B3pez%20%28%40ana%29`) porque S3 solo admite ASCII.
- **Dentro de la foto (EXIF)**, para que los datos viajen con ella si la descargas: el mismo texto en `ImageDescription` y `XPComment`, y el remitente en `Artist` y `XPAuthor`. En Windows se ven en *Propiedades → Detalles* (*Comentarios* y *Autores*). Solo en las fotos normales, que Telegram comprime y entrega sin EXIF; la imagen no se vuelve a comprimir. Las fotos enviadas como archivo se guardan tal cual.

## Puesta en marcha

**La guía completa está en [DEPLOY.md](DEPLOY.md)**: elegir VPS, dominio gratuito con DuckDNS, Google Drive, OneDrive, despliegue automático, copias de seguridad y problemas frecuentes.

En resumen, en un VPS con Ubuntu o Debian (amd64 o arm64) y los puertos 80/443 abiertos:

```bash
curl -fsSL https://raw.githubusercontent.com/jpchaconr/telebot-photos/HEAD/deploy/install.sh | sudo bash
```

El instalador:

- instala Docker y configura el cortafuegos;
- te pide el token de [@BotFather](https://t.me/BotFather) (con el *privacy mode* desactivado), el tamaño máximo de archivo y el dominio. Con **20 MB** no hace falta nada más; con **2 GB** también pide el `api_id`/`api_hash` de <https://my.telegram.org>;
- genera `.env` y la clave de cifrado;
- arranca el bot y Caddy (HTTPS automático), y en el modo 2 GB también la Bot API local.

Se puede volver a ejecutar para añadir Google Drive u OneDrive. Las actualizaciones llegan con GitHub Actions (imagen en `ghcr.io/jpchaconr/telebot-photos`) o con `deploy/update.sh`.

> ⚠️ **Guarda una copia de `.env`** (contiene `ENCRYPTION_KEY`). Sin la clave, las configuraciones guardadas no se pueden leer y cada grupo tendría que configurarse de nuevo.

Si solo configuras Google o solo Microsoft, el otro proveedor simplemente no aparece. S3 está siempre disponible porque cada grupo introduce sus propias claves.

## Uso en un grupo

1. Añade el bot al grupo.
2. Un administrador escribe **`/configurar`** y pulsa *Configurar en privado*. El enlace solo funciona para él y caduca en 15 minutos.
3. En el chat privado elige el proveedor:
   - **Google Drive / OneDrive**: pulsa *Autorizar*, inicia sesión y acepta. El bot crea la carpeta del grupo y avisa en el grupo.
   - **S3**: el bot pide endpoint, región, bucket, Access Key ID, Secret Access Key y prefijo. **Borra los mensajes con las claves** en cuanto los lee y hace una escritura de prueba antes de guardar nada.
4. A partir de ese momento cada archivo que se envíe al grupo se guarda y recibe una reacción 👍.

| Comando | Quién | Qué hace |
|---|---|---|
| `/configurar` | admins | Conecta o cambia el almacenamiento del grupo |
| `/estado` | todos | Proveedor, carpeta, archivos guardados, pendientes y con error |
| `/enlace` | todos | Enlace a la carpeta (Drive/OneDrive) o ubicación en S3 |
| `/enlace publico` · `/enlace privado` | admins | Google Drive: abre la carpeta en **solo lectura** a cualquiera con el enlace, para que nadie tenga que pedir acceso, o la vuelve a cerrar |
| `/galeria` | todos | Enlace a la galería web (la primera vez la activa un admin) |
| `/pausar` · `/reanudar` | admins | Deja de guardar o vuelve a hacerlo |
| `/desconectar` | admins | Borra las credenciales y la cola del grupo (no toca lo ya guardado) |
| `/ayuda` | todos | Ayuda |

Si alguien revoca el acceso a la cuenta o borra la carpeta raíz, el bot avisa en el grupo una sola vez y guarda los archivos pendientes en cuanto un admin vuelve a usar `/configurar`.

### Galería web

Una página para ver lo guardado sin entrar en la cuenta de Drive, OneDrive o S3:

- **Cómo se entra**: un administrador escribe `/galeria` en el grupo y el bot responde con un enlace secreto (`https://DOMAIN/g/…`). Después, cualquier miembro puede pedirlo con `/galeria`.
- **Qué muestra**: las fotos, vídeos y GIFs, en una cuadrícula agrupada por mes, con filtros por persona y por mes. El visor a pantalla completa indica quién lo envió, cuándo y con qué comentario, y tiene un botón para descargar el original. Los audios, notas de voz y documentos no aparecen, salvo las imágenes y vídeos enviados como archivo.
- **Solo lectura**: la web no puede subir, cambiar ni borrar nada.

Seguridad:

- **Quien tenga el enlace puede ver la galería.** El enlace es largo y aleatorio (imposible de adivinar), y el bot lo envía como contenido protegido, para que no se pueda reenviar desde Telegram. Aun así, que nadie lo comparta fuera del grupo.
- **Cambiar el enlace o desactivar la galería**: los botones del mensaje de `/galeria` lo hacen al instante (solo admins). El enlace anterior deja de funcionar.
- **Las credenciales nunca llegan al navegador**:
  - **S3**: la web redirige a un enlace prefirmado que caduca a los 10 minutos y solo abre ese archivo. Las claves necesitan permiso de lectura (`s3:GetObject`); si no lo tienen, el bot avisa al activar la galería.
  - **OneDrive**: redirige a un enlace temporal de descarga.
  - **Drive**: el servidor hace de intermediario.
- **Vistas previas**: las miniaturas (unos 320 px) son las que ya genera Telegram. Se descargan en `THUMB_DIR` la primera vez que alguien abre la galería, y se borran al desactivarla o con `/desconectar`.

### Datos para proveedores S3 habituales

| Servicio | Endpoint | Región |
|---|---|---|
| AWS S3 | escribe `aws` | la del bucket, p. ej. `eu-west-1` |
| Cloudflare R2 | `https://<ACCOUNT_ID>.r2.cloudflarestorage.com` | `auto` |
| Backblaze B2 | `https://s3.<región>.backblazeb2.com` | p. ej. `us-west-004` |
| MinIO | `https://minio.tudominio.com` | `us-east-1` (o la configurada) |

Usa claves limitadas a ese bucket, con permiso de escritura y, si vais a usar la galería web, también de lectura (`s3:GetObject`).

## Variables de entorno

| Variable | Obligatoria | Descripción |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | sí | Token de BotFather |
| `TELEGRAM_BOT_API` | no | `cloud`: API de Telegram, archivos de hasta 20 MB. `local`: servidor local de la Bot API, hasta 2 GB. Para cambiarlo, vuelve a ejecutar el instalador (ver DEPLOY.md) |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | con `local` | Para el servidor local de la Bot API |
| `COMPOSE_FILE` | no | Ficheros de Docker Compose que se usan. Lo escribe el instalador: añade `docker-compose.bot-api.yml` (modo 2 GB) y `docker-compose.build.yml` (imagen construida en el servidor) |
| `DOMAIN` | sí | Nombre público del servidor (p. ej. `nombre.duckdns.org`), para Caddy y los redirect URI |
| `ACME_EMAIL` | sí (Docker) | Email de la cuenta de Let's Encrypt |
| `PUBLIC_BASE_URL` | no | URL pública si no es `https://DOMAIN` |
| `ENCRYPTION_KEY` | sí | Clave Fernet para cifrar credenciales (`openssl rand -base64 32 \| tr '+/' '-_'`) |
| `DUCKDNS_TOKEN` | con DuckDNS | Lo usa `deploy/duckdns-update.sh` para mantener el dominio apuntando a la IP |
| `CONTACT_EMAIL` | no | Email de contacto que aparece en `/privacy` |
| `BOT_TAG`, `BOT_IMAGE` | no | Imagen del bot (`ghcr.io/jpchaconr/telebot-photos:latest` por defecto); `deploy/update.sh` cambia la etiqueta |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | no | Habilitan Google Drive |
| `MS_CLIENT_ID`, `MS_CLIENT_SECRET`, `MS_TENANT` | no | Habilitan OneDrive (`MS_TENANT=common` por defecto) |
| `BOT_API_URL` | no | Servidor local de la Bot API; `docker-compose.bot-api.yml` ya lo define. Vacío = API en la nube (límite 20 MB) |
| `TIMEZONE` | no | Zona horaria de nombres y carpetas (`UTC` por defecto) |
| `UPLOAD_CONCURRENCY` | no | Subidas simultáneas (3) |
| `MAX_ATTEMPTS` | no | Reintentos antes de marcar un archivo como fallido (5) |
| `SUCCESS_REACTION` | no | Reacción al guardar (👍; vacío = ninguna) |
| `ALLOWED_CONFIGURATORS` | no | IDs de Telegram que pueden usar `/configurar`, separados por comas (vacío = cualquier admin) |
| `DATABASE_PATH`, `TEMP_DIR`, `THUMB_DIR` | no | Rutas de la base de datos, los temporales y las vistas previas de la galería (`/data/...` en Docker) |
| `WEB_HOST`, `WEB_PORT` | no | Dónde escucha el servidor web (`0.0.0.0:8080`) |
| `LOG_LEVEL` | no | `INFO` por defecto |

## Limitaciones

- Solo se guarda lo que se envía **mientras el bot está en el grupo**: la API de bots no permite leer el historial anterior.
- Tamaño máximo por archivo: 20 MB con la API de Telegram, o 2 GB con el servidor local de la Bot API. Son los límites de Telegram para bots.
- Los stickers no se guardan, y editar un mensaje para cambiar su archivo no actualiza la copia guardada.
- En un álbum, Telegram pone el comentario en una sola de las fotos: las demás se guardan sin él. Editar el comentario después tampoco actualiza la copia.
- Para conectar Google Drive u OneDrive, `PUBLIC_BASE_URL` tiene que ser accesible desde el navegador de quien autoriza.

## Desarrollo

```bash
python3 -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest          # incluye una prueba de extremo a extremo con una Bot API falsa y S3 simulado
ruff check src tests && ruff format --check src tests
```

### Probar el bot en tu ordenador

No hace falta un VPS: el bot pide los mensajes a Telegram (*polling*), así que no necesita IP pública ni HTTPS. Con un S3 local se prueba el recorrido completo. Solo Google Drive y OneDrive necesitan una URL pública (ver más abajo).

1. **Crea un bot solo para pruebas** en [@BotFather](https://t.me/BotFather) (`/newbot`) y desactiva el *privacy mode* (`/setprivacy` → *Disable*).
   - No uses el token del VPS: si dos procesos piden mensajes con el mismo token, Telegram corta uno de ellos con un error `Conflict`.
   - Si el bot ya estaba en el grupo cuando desactivaste el *privacy mode*, sácalo y vuelve a añadirlo.
2. **Crea un `.env`** en la raíz del proyecto:
   ```ini
   TELEGRAM_BOT_TOKEN='token-del-bot-de-pruebas'
   ENCRYPTION_KEY='...'
   PUBLIC_BASE_URL='http://localhost:8080'
   TIMEZONE='Europe/Madrid'
   ```
   - Genera la clave con `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
   - Sin `BOT_API_URL` se usa la API de Telegram, con el límite de 20 MB, igual que en el modo de 20 MB del VPS.
   - La base de datos se crea en `data/bot.db`.
3. **Arranca un S3 local** con moto (se instala con `.[dev]`) en otra terminal, con el venv activado, y crea el bucket una vez:
   ```bash
   moto_server -p 9000
   python -c "import boto3; boto3.client('s3', endpoint_url='http://localhost:9000', aws_access_key_id='test', aws_secret_access_key='test', region_name='us-east-1').create_bucket(Bucket='fotos')"
   ```
4. **Arranca el bot**: `python -m telebot_photos`.
5. **Configura el grupo**: añade el bot a un grupo de prueba y escribe `/configurar` → **S3**. Cuando te lo pida en privado, envía:
   - endpoint: `http://localhost:9000`
   - región: `us-east-1`
   - bucket: `fotos`
   - claves: `test` y `test`
6. **Envía una foto al grupo**: el bot reacciona con 👍. Lo guardado se ve en <http://localhost:9000/fotos> o en el panel <http://localhost:9000/moto-api/>.
7. **Abre la galería**: escribe `/galeria` en el grupo y abre el enlace (`http://localhost:8080/g/…`). Las miniaturas se piden a Telegram la primera vez.

**Ten en cuenta:**

- **moto guarda todo en memoria**: al pararlo se pierden los archivos y tienes que volver a crear el bucket. Si quieres que se conserven, usa la capa gratuita de Backblaze B2 o Cloudflare R2 ([endpoints](#datos-para-proveedores-s3-habituales)).
- **Google Drive y OneDrive** necesitan una URL pública para el OAuth:
  1. Arranca un túnel, por ejemplo `cloudflared tunnel --url http://localhost:8080`.
  2. Pon la URL que te dé en `PUBLIC_BASE_URL` y en los redirect URI de las consolas de Google y Microsoft (apartados 5 y 6 de [DEPLOY.md](DEPLOY.md)).

  Con el túnel rápido, la URL cambia cada vez que lo arrancas.

### Imagen de Docker en local

Para construir la imagen en local en vez de descargarla de GHCR:

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

Añade `-f docker-compose.bot-api.yml` (después de `docker-compose.yml`) para el modo de 2 GB.

### Estructura

Código (`src/telebot_photos/`):

| Módulo | Contenido |
|---|---|
| `__main__.py` | Arranque: bot (polling), servidor web y workers en el mismo event loop |
| `bot/setup.py` | `/configurar`, deep link y conversación privada (OAuth y S3) |
| `bot/media.py` | Captura de la multimedia y encolado |
| `bot/admin.py` | `/estado`, `/enlace`, `/galeria`, `/pausar`, `/reanudar`, `/desconectar`, migración de grupos |
| `worker.py` | Descarga desde Telegram, subida, reintentos, reacciones y avisos |
| `exif.py` | Remitente y comentario en el EXIF de las fotos, sin recomprimirlas |
| `storage/` | Backends `gdrive.py`, `onedrive.py` y `s3.py`, con subidas por partes |
| `oauth/` | Cliente OAuth 2.0 y refresco de tokens |
| `web.py` | Página principal, `/privacy`, callbacks OAuth y `/health` |
| `gallery.py`, `static/` | Galería web de solo lectura (`/g/<enlace>`): página, API, miniaturas y archivos |
| `thumbs.py` | Vistas previas de la galería, descargadas de Telegram la primera vez que se ven |
| `db.py` | SQLite: configuración por chat, tokens de un solo uso, cola de subidas y galerías |

Despliegue:

| Fichero | Contenido |
|---|---|
| `docker-compose.yml` | Bot y Caddy: archivos de hasta 20 MB |
| `docker-compose.bot-api.yml` | Añade el servidor local de la Bot API: archivos de hasta 2 GB |
| `deploy/install.sh` | Instalador del VPS (Docker, cortafuegos, `.env`, DuckDNS, arranque, usuario de despliegue) |
| `deploy/update.sh` | Actualizar o volver a una versión (lo usa también GitHub Actions por SSH) |
| `deploy/backup.sh`, `deploy/restore.sh` | Copias de seguridad de la base de datos |
| `.github/workflows/ci.yml` | Lint, tests y comprobación de scripts y compose en cada push |
| `.github/workflows/deploy.yml` | Imagen multi-arch en GHCR y despliegue en el VPS |
