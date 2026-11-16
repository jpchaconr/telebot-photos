# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Telegram bot that saves every photo, video, GIF, audio, voice note and document posted in a group to the cloud storage chosen **per group** (Google Drive or OneDrive via OAuth, or any S3-compatible bucket), and serves a read-only web gallery of each group's saved media. Python 3.11+, asyncio, python-telegram-bot 22, aiohttp, httpx, boto3, aiosqlite. README.md (user docs, local testing) and DEPLOY.md (VPS deployment) are in Spanish.

## Commands

```bash
pip install -e ".[dev]"                            # into .venv (Windows: .venv\Scripts\activate)
pytest                                             # all tests, including the e2e one
pytest tests/test_worker.py::test_name             # single test
ruff check src tests && ruff format --check src tests   # what CI runs (line length 100)
python -m telebot_photos                           # run the bot; reads .env from the cwd
```

`pytest` config lives in `pyproject.toml` (`asyncio_mode = "auto"`, `pythonpath = ["src"]`), so async tests need no decorator. CI (`.github/workflows/ci.yml`) also runs `shellcheck -x -S style deploy/*.sh` and `docker compose config` on every compose-file combination; keep those passing when editing `deploy/` or compose files.

Local end-to-end testing uses a separate test bot token, `moto_server -p 9000` as a fake S3, and `PUBLIC_BASE_URL=http://localhost:8080`; see "Probar el bot en tu ordenador" in README.md.

## Architecture

One process, one event loop (`__main__.serve`): PTB polling, the aiohttp web server and N upload worker tasks run side by side. Long-lived objects are bundled in `services.Services`, stored in `application.bot_data["services"]` and fetched by handlers with `get_services(context)`.

Flow of a file:
1. `bot/media.py` extracts the media from a group message and, if the chat is configured and not paused, inserts a row into the SQLite `jobs` table (`UNIQUE(chat_id, file_unique_id)` is the dedup mechanism) and calls `worker.notify()`. Files over the cloud Bot API's 20 MB limit are rejected here with a warning in the group.
2. `worker.UploadWorker` claims jobs from the DB, downloads from Telegram (or reads the file in place when a local Bot API server shares its volume), builds the remote folder/name via `naming.py` (monthly folders, `YYYYMMDD-HHMMSS_sender_msgid[_name].ext`), writes sender/caption into photo EXIF (`exif.py`, best effort, no recompression) and calls the chat's backend.
3. Errors drive the retry policy through the exception type from `storage/base.py`: `StorageError` → retried with exponential backoff up to `MAX_ATTEMPTS`; `PermanentStorageError` → failed and reported in the group; `StorageAuthError` → chat status set to `reauth_required`, job retried without counting the attempt. On success the job stores the backend's `remote_ref` plus its `location` (`storage/registry.storage_location`), and the bot reacts to the message.

Storage backends (`storage/`) implement the `StorageBackend` protocol: `upload(UploadRequest) -> str`, `media(MediaRequest) -> MediaRedirect | MediaStream` (read access for the gallery) and `aclose()`. `storage/registry.BackendRegistry` builds one per chat from its `ChatConfig` and caches it keyed by `configured_at`, so reconfiguring a chat invalidates it automatically. Drive and OneDrive go through `storage/_http.AuthorizedHttp`, which uses `oauth/base.TokenManager` to refresh tokens and persist new ones (encrypted) back to the DB. Adding a provider means touching `PROVIDER_NAMES`, `BackendRegistry.create`, `storage_location`, `oauth/providers.OAUTH_FOR_PROVIDER` (if OAuth) and the setup conversation.

Web gallery (`gallery.py`, `static/gallery.{js,css}`, `thumbs.py`): `/galeria` (`bot/admin.py`) creates a secret per-chat token in the `galleries` table, and `/g/<token>` serves a page plus a JSON API listing done `jobs` rows (no provider listing calls). The gallery only shows jobs whose `location` matches the chat's current storage, so files left in a previous storage after reconfiguring disappear from it. File bytes come from `backend.media()`: S3 and OneDrive return a short-lived redirect URL, Drive streams through the server (Range supported). Credentials never reach the browser, and the gallery is strictly read-only. Previews are Telegram's own ~320 px thumbnails (`thumb_file_id`, captured in `bot/media.py`), downloaded lazily into `THUMB_DIR/<chat_id>/` through `worker.fetch_file`. Only safe types are shown inline (`GALLERY_DOCUMENT_TYPES`, never SVG/HTML), and every `/g/` response gets the strict security headers and CSP from `gallery.add_security_headers`.

Configuration (`bot/setup.py`): `/configurar` in a group creates a one-time setup token and deep-links the admin to a private chat (`/start setup_<token>`), so credentials are never typed in the group. That private `ConversationHandler` must be registered before the other handlers. OAuth providers finish in `web.py`'s `/oauth/{provider}/callback`, validated by a one-time `oauth_states` row; S3 collects endpoint/region/bucket/keys step by step and deletes the messages carrying secrets.

Persistence (`db.py`): a single aiosqlite connection in WAL mode, every public method serialized by one `asyncio.Lock`. Credentials are Fernet-encrypted with `ENCRYPTION_KEY` (`crypto.py`). There is no migration framework: the schema is `CREATE TABLE IF NOT EXISTS`, and new `jobs` columns are added through `JOB_MIGRATIONS` at startup, so new columns must be nullable or have defaults (old rows exist, e.g. `sender_label` falls back to `sender_name`, and a NULL `location` counts as current). Indexes on migrated columns go in `INDEXES`, which runs after the migrations. Per-chat state lives in `chats`, `jobs`, `galleries` and the thumbnail directory: deleting a chat (`/desconectar`) or migrating it (group upgraded to supergroup) must cover all of them: the tables in `Database.delete_chat`/`migrate_chat`, the thumbnails via `thumbs.delete_chat`/`move_chat` called from `bot/admin.py`.

Settings (`config.py`) come from environment / `.env` via pydantic-settings; `DOMAIN` or `PUBLIC_BASE_URL` is required, and `BOT_API_URL` switches to a local Bot API server (2 GB files, `local_mode`).

## Conventions

- User-facing text (bot messages, web pages, setting error messages, README/DEPLOY) is in Spanish; code, comments, docstrings and log messages are in English.
- Tests never hit the network: `tests/conftest.py` provides `settings`, `db`, `FakeBot`, `FakeWorker`, `configure_chat` and `new_job`; HTTP backends are mocked with `respx`, S3 with `moto`. `tests/test_e2e.py` runs the real `serve()` against a fake Bot API aiohttp server; `tests/test_gallery.py` covers the gallery's routes and access rules.
- Provider quirks are handled in code with explanatory comments (e.g. OneDrive rejects descriptions in upload sessions, so it sets them with a later PATCH and stops trying after a 400). Read the backend's comments before changing upload logic.

## Deployment

Docker image published to GHCR by `.github/workflows/deploy.yml`, which also deploys over SSH using `deploy/update.sh`. `docker-compose.yml` runs the bot plus Caddy (HTTPS for the OAuth callbacks); add `docker-compose.bot-api.yml` for the local Bot API server (the bot runs as uid/gid 101 to share its volume) and `docker-compose.build.yml` to build locally. Only Caddy may publish ports (Docker-published ports bypass ufw).
