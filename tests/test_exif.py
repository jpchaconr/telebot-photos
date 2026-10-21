from io import BytesIO
from pathlib import Path

from PIL import Image

from telebot_photos.exif import add_exif, write_tagged_copy

DESCRIPTION = "Enviado por: Ana López (@ana)\nComentario: ¡Feliz cumple! 🎂"
ARTIST = "Ana López (@ana)"


def jpeg(**save_options) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (16, 8), (200, 40, 90)).save(buffer, "JPEG", **save_options)
    return buffer.getvalue()


def read_exif(data: bytes) -> dict[int, object]:
    with Image.open(BytesIO(data)) as image:
        return dict(image.getexif())


def utf8(value: object) -> str:
    # Pillow decodes EXIF ASCII as Latin-1; the bytes are UTF-8.
    assert isinstance(value, str)
    return value.encode("latin-1").decode()


def utf16(value: object) -> str:
    assert isinstance(value, bytes)
    return value.decode("utf-16-le").rstrip("\0")


def test_tags_are_readable_and_the_image_is_untouched():
    original = jpeg()
    assert original[2:4] == b"\xff\xe0"  # Pillow writes a JFIF segment first
    tagged = add_exif(original, description=DESCRIPTION, artist=ARTIST)

    tags = read_exif(tagged)
    assert utf8(tags[0x010E]) == DESCRIPTION
    assert utf8(tags[0x013B]) == ARTIST
    assert utf16(tags[0x9C9C]) == DESCRIPTION
    assert utf16(tags[0x9C9D]) == ARTIST

    # The EXIF segment goes after JFIF and nothing else changes.
    jfif_end = 4 + int.from_bytes(original[4:6], "big")
    assert tagged[jfif_end : jfif_end + 2] == b"\xff\xe1"
    exif_end = jfif_end + 2 + int.from_bytes(tagged[jfif_end + 2 : jfif_end + 4], "big")
    assert tagged[:jfif_end] + tagged[exif_end:] == original
    with Image.open(BytesIO(tagged)) as image, Image.open(BytesIO(original)) as before:
        assert image.tobytes() == before.tobytes()


def test_only_the_sender_and_short_values():
    tags = read_exif(add_exif(jpeg(), description=None, artist="Bo"))
    assert utf8(tags[0x013B]) == "Bo"
    assert utf16(tags[0x9C9D]) == "Bo"
    assert 0x010E not in tags and 0x9C9C not in tags


def test_longest_caption_fits_in_one_segment():
    caption = "🎂" * 4096  # Telegram Premium caption limit, 4 bytes per character
    tags = read_exif(add_exif(jpeg(), description=caption, artist=ARTIST))
    assert utf16(tags[0x9C9C]) == "🎂" * 4000


def test_never_touches_existing_exif_or_other_files():
    with_exif = add_exif(jpeg(), description="primero", artist=None)
    assert add_exif(with_exif, description="segundo", artist=None) is None
    assert add_exif(b"\x89PNG\r\n\x1a\n", description="x", artist=None) is None
    assert add_exif(b"\xff\xd8 not a jpeg", description="x", artist=None) is None
    assert add_exif(jpeg(), description=None, artist=None) is None


def test_write_tagged_copy(tmp_path: Path):
    source, target = tmp_path / "in.jpg", tmp_path / "out.jpg"
    source.write_bytes(jpeg())
    size = write_tagged_copy(source, target, description="hola", artist=ARTIST)
    assert size == target.stat().st_size
    assert utf8(read_exif(target.read_bytes())[0x010E]) == "hola"

    source.write_bytes(b"data")
    assert write_tagged_copy(source, tmp_path / "none.jpg", description="x", artist=None) is None
    assert not (tmp_path / "none.jpg").exists()
