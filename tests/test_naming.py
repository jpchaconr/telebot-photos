from datetime import datetime
from zoneinfo import ZoneInfo

from telebot_photos.naming import build_remote_name, extension_for, sanitize

MADRID = ZoneInfo("Europe/Madrid")
SENT_AT = datetime(2026, 10, 18, 13, 30, 5, tzinfo=ZoneInfo("UTC")).timestamp()


def test_photo_name_uses_local_time_and_month_folder():
    remote = build_remote_name(
        kind="photo",
        sent_at=SENT_AT,
        tz=MADRID,
        sender="Ana López",
        message_id=42,
        file_name=None,
        mime_type="image/jpeg",
    )
    assert remote.folder == "2026-10"
    assert remote.name == "20261018-153005_Ana_López_42.jpg"
    assert remote.mime_type == "image/jpeg"
    assert remote.path == "2026-10/20261018-153005_Ana_López_42.jpg"


def test_document_keeps_original_name_sanitized():
    remote = build_remote_name(
        kind="document",
        sent_at=SENT_AT,
        tz=MADRID,
        sender="ana",
        message_id=7,
        file_name='C:\\fotos\\viaje: "playa"?.PDF',
        mime_type="application/pdf",
    )
    assert remote.name == "20261018-153005_ana_7_viaje_playa_.PDF"


def test_document_without_extension_gets_one_from_mime():
    remote = build_remote_name(
        kind="document",
        sent_at=SENT_AT,
        tz=MADRID,
        sender="ana",
        message_id=7,
        file_name="IMG_0001",
        mime_type="image/heic",
    )
    assert remote.name.endswith("_7_IMG_0001.heic")


def test_defaults_when_telegram_gives_no_mime_type():
    remote = build_remote_name(
        kind="video_note",
        sent_at=SENT_AT,
        tz=MADRID,
        sender="",
        message_id=1,
        file_name=None,
        mime_type=None,
    )
    assert remote.name == "20261018-153005_anonimo_1.mp4"
    assert remote.mime_type == "video/mp4"


def test_sanitize_trims_long_names_but_keeps_extension():
    name = sanitize("a" * 200 + ".jpeg", max_length=40)
    assert len(name) <= 40
    assert name.endswith(".jpeg")


def test_sanitize_never_returns_empty():
    assert sanitize("../..", fallback="x") == "x"
    assert sanitize("  ") == "archivo"


def test_extension_for_unknown_mime_falls_back_to_kind():
    assert extension_for("voice", "application/x-unknown-type") == "ogg"
    assert extension_for("video", "video/quicktime") == "mov"
