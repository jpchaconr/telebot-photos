from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from telebot_photos.storage.base import PermanentStorageError, UploadRequest
from telebot_photos.storage.s3 import S3Backend, object_key

CREDENTIALS = {"access_key_id": "AK", "secret_access_key": "SK"}


@pytest.fixture
def aws():
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="fotos")
        yield


def test_object_key_skips_empty_parts():
    assert object_key("", "2026-10", "a.jpg") == "2026-10/a.jpg"
    assert object_key("/telegram/Familia/", "2026-10", "a.jpg") == "telegram/Familia/2026-10/a.jpg"


async def test_upload_and_check(aws, tmp_path: Path):
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"jpeg-bytes")
    backend = S3Backend(
        CREDENTIALS, {"bucket": "fotos", "prefix": "telegram/Familia", "region": "us-east-1"}
    )
    await backend.check()
    key = await backend.upload(
        UploadRequest(source, "2026-10", "a.jpg", "image/jpeg", 10, description="¡Playa!")
    )
    assert key == "telegram/Familia/2026-10/a.jpg"

    obj = boto3.client("s3", region_name="us-east-1").get_object(Bucket="fotos", Key=key)
    assert obj["Body"].read() == b"jpeg-bytes"
    assert obj["ContentType"] == "image/jpeg"
    assert obj["Metadata"]["caption"] == "%C2%A1Playa%21"
    await backend.aclose()


async def test_missing_bucket_is_permanent(aws, tmp_path: Path):
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"x")
    backend = S3Backend(CREDENTIALS, {"bucket": "no-existe", "region": "us-east-1"})
    with pytest.raises(PermanentStorageError):
        await backend.check()
    with pytest.raises(PermanentStorageError):
        await backend.upload(UploadRequest(source, "2026-10", "a.jpg", "image/jpeg", 1))
