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
                           │ descarga (servidor local de la Bot API, hasta 2 GB)
                           │ sube a la nube configurada para ese grupo
                           └─▶ reacciona 👍 al mensaje (o avisa si falla)
```

- **Configuración por chat**: un admin escribe `/configurar` en el grupo y el bot le lleva al chat privado, para que las credenciales nunca se escriban en el grupo.
- **Credenciales cifradas** en la base de datos con `ENCRYPTION_KEY` (Fernet).
- **Cola persistente** con reintentos y backoff exponencial: si el servidor se reinicia o la nube falla un rato, no se pierde nada.
- **Sin duplicados**: el mismo archivo reenviado varias veces al grupo se guarda una sola vez.
- Usa el **servidor local de la Bot API** para descargar archivos de hasta 2 GB; la API en la nube de Telegram limita a los bots a 20 MB.

Los archivos se organizan por mes:

```
Telegram - Familia/                       (Drive)   ·  telegram/Familia/ (S3)
└── 2026-10/
    ├── 20261018-153005_ana_1234.jpg
    ├── 20261018-153112_luis_1240.mp4
    └── 20261018-160000_ana_1251_presupuesto.pdf
```

El pie de foto se guarda como descripción del archivo en Drive y OneDrive, y como metadato `caption` en S3.

## Puesta en marcha

Necesitas un servidor con Docker y Docker Compose, un dominio que apunte a él y los puertos 80 y 443 abiertos (para el HTTPS de los callbacks OAuth).

### 1. Crear el bot

1. Habla con [@BotFather](https://t.me/BotFather), usa `/newbot` y guarda el token.
2. **Desactiva el privacy mode**: `/setprivacy` → elige el bot → `Disable`. Sin esto el bot no ve las fotos que se envían en el grupo. La alternativa es hacerlo administrador en cada grupo.

### 2. Credenciales del servidor local de la Bot API

1. Entra en <https://my.telegram.org>, ve a *API development tools* y crea una aplicación.
2. Copia el `api_id` y el `api_hash`.
3. Desconecta el bot de la API en la nube, **una sola vez**, antes de arrancarlo con el servidor local:
   ```bash
   curl https://api.telegram.org/bot<TOKEN>/logOut
   ```
   Durante los 10 minutos siguientes no podrás volver a usarlo con la API en la nube.

### 3. Google Drive (opcional)

1. En [Google Cloud Console](https://console.cloud.google.com/) crea un proyecto y habilita la **Google Drive API**.
2. Configura la **pantalla de consentimiento OAuth**: tipo *External*, y **publícala** (*In production*).
   - En modo *Testing* los tokens caducan a los 7 días y solo pueden entrar los usuarios de prueba.
   - El bot solo pide el permiso `drive.file`, que no requiere verificación de Google. Con él el bot solo ve los archivos y carpetas que él mismo crea.
3. En *Credentials* crea un **OAuth client ID** de tipo *Web application* con este redirect URI:
   ```
   https://TU_DOMINIO/oauth/google/callback
   ```
4. Copia el *Client ID* y el *Client secret*.

### 4. OneDrive (opcional)

1. En [Azure Portal](https://portal.azure.com/) ve a **Microsoft Entra ID → App registrations → New registration**.
2. En *Supported account types* elige **Accounts in any organizational directory and personal Microsoft accounts**.
3. Añade un redirect URI de tipo **Web**:
   ```
   https://TU_DOMINIO/oauth/microsoft/callback
   ```
4. En *Certificates & secrets* crea un **client secret** y copia su *Value*. Caduca: apúntate cuándo renovarlo.
5. Copia el *Application (client) ID*.

Los permisos delegados (`Files.ReadWrite`, `offline_access`, `User.Read`) se piden al autorizar; no hace falta añadirlos a mano.

### 5. Configurar y arrancar

```bash
git clone <este repo> && cd telebot-photos
cp .env.example .env
# Genera la clave de cifrado y pégala en ENCRYPTION_KEY:
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
nano .env          # token, api_id/api_hash, dominio y credenciales OAuth
docker compose up -d --build
docker compose logs -f bot
```

Comprueba que `https://TU_DOMINIO/health` responde `{"status": "ok"}`.

> ⚠️ **Guarda una copia de `ENCRYPTION_KEY`** y del volumen `bot-data` (la base de datos). Sin la clave, las configuraciones guardadas no se pueden leer y cada grupo tendría que configurarse de nuevo.

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
| `/pausar` · `/reanudar` | admins | Deja de guardar o vuelve a hacerlo |
| `/desconectar` | admins | Borra las credenciales y la cola del grupo (no toca lo ya guardado) |
| `/ayuda` | todos | Ayuda |

Si alguien revoca el acceso a la cuenta o borra la carpeta raíz, el bot avisa en el grupo una sola vez y guarda los archivos pendientes en cuanto un admin vuelve a usar `/configurar`.

### Datos para proveedores S3 habituales

| Servicio | Endpoint | Región |
|---|---|---|
| AWS S3 | escribe `aws` | la del bucket, p. ej. `eu-west-1` |
| Cloudflare R2 | `https://<ACCOUNT_ID>.r2.cloudflarestorage.com` | `auto` |
| Backblaze B2 | `https://s3.<región>.backblazeb2.com` | p. ej. `us-west-004` |
| MinIO | `https://minio.tudominio.com` | `us-east-1` (o la configurada) |

Usa claves limitadas a ese bucket, con permiso de escritura.

## Variables de entorno

| Variable | Obligatoria | Descripción |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | sí | Token de BotFather |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | sí (Docker) | Para el servidor local de la Bot API |
| `DOMAIN` | sí (Docker) | Dominio para el certificado HTTPS de Caddy |
| `PUBLIC_BASE_URL` | sí | URL pública, base de los redirect URI (`https://DOMAIN`) |
| `ENCRYPTION_KEY` | sí | Clave Fernet para cifrar credenciales |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | no | Habilitan Google Drive |
| `MS_CLIENT_ID`, `MS_CLIENT_SECRET`, `MS_TENANT` | no | Habilitan OneDrive (`MS_TENANT=common` por defecto) |
| `BOT_API_URL` | no | Servidor local de la Bot API; docker-compose ya lo define. Vacío = API en la nube (límite 20 MB) |
| `TIMEZONE` | no | Zona horaria de nombres y carpetas (`UTC` por defecto) |
| `UPLOAD_CONCURRENCY` | no | Subidas simultáneas (3) |
| `MAX_ATTEMPTS` | no | Reintentos antes de marcar un archivo como fallido (5) |
| `SUCCESS_REACTION` | no | Reacción al guardar (👍; vacío = ninguna) |
| `ALLOWED_CONFIGURATORS` | no | IDs de Telegram que pueden usar `/configurar`, separados por comas (vacío = cualquier admin) |
| `DATABASE_PATH`, `TEMP_DIR` | no | Rutas de la base de datos y temporales (`/data/...` en Docker) |
| `WEB_HOST`, `WEB_PORT` | no | Dónde escucha el servidor web (`0.0.0.0:8080`) |
| `LOG_LEVEL` | no | `INFO` por defecto |

## Limitaciones

- Solo se guarda lo que se envía **mientras el bot está en el grupo**: la API de bots no permite leer el historial anterior.
- Tamaño máximo de 2 GB por archivo, que es el límite de Telegram para bots con servidor local.
- Los stickers no se guardan, y editar un mensaje para cambiar su archivo no actualiza la copia guardada.
- Para conectar Google Drive u OneDrive, `PUBLIC_BASE_URL` tiene que ser accesible desde el navegador de quien autoriza.

## Desarrollo

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest          # incluye una prueba de extremo a extremo con una Bot API falsa y S3 simulado
ruff check src tests && ruff format --check src tests
```

Para probar sin Docker, crea un `.env` y ejecuta `python -m telebot_photos`:

- Sin `BOT_API_URL` se usa la API en la nube de Telegram, con el límite de 20 MB y sin `logOut`.
- Para OAuth necesitas una URL pública: puedes usar un túnel como `cloudflared tunnel --url http://localhost:8080` y poner esa URL en `PUBLIC_BASE_URL` y en los redirect URI. Con S3 no hace falta.

Estructura del código (`src/telebot_photos/`):

| Módulo | Contenido |
|---|---|
| `__main__.py` | Arranque: bot (polling), servidor web y workers en el mismo event loop |
| `bot/setup.py` | `/configurar`, deep link y conversación privada (OAuth y S3) |
| `bot/media.py` | Captura de la multimedia y encolado |
| `bot/admin.py` | `/estado`, `/enlace`, `/pausar`, `/reanudar`, `/desconectar`, migración de grupos |
| `worker.py` | Descarga desde Telegram, subida, reintentos, reacciones y avisos |
| `storage/` | Backends `gdrive.py`, `onedrive.py` y `s3.py`, con subidas por partes |
| `oauth/` | Cliente OAuth 2.0 y refresco de tokens |
| `web.py` | Callbacks OAuth y `/health` |
| `db.py` | SQLite: configuración por chat, tokens de un solo uso y cola de subidas |
