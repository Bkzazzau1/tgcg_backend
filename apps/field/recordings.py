"""Where LiveKit puts recordings of live incident video: an S3-compatible
bucket (AWS S3, MinIO, Spaces...). The server copies each recording onto
its incident, then deletes it from the bucket.

Set LIVE_RECORDING_S3_ENDPOINT, LIVE_RECORDING_S3_BUCKET,
LIVE_RECORDING_S3_ACCESS_KEY and LIVE_RECORDING_S3_SECRET; without them
live video is watched but not recorded.
"""

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager

import boto3
from botocore.config import Config
from django.conf import settings

from apps.meetings import livekit


def configured() -> bool:
    return livekit.configured() and all(
        (
            settings.LIVE_RECORDING_S3_ENDPOINT,
            settings.LIVE_RECORDING_S3_BUCKET,
            settings.LIVE_RECORDING_S3_ACCESS_KEY,
            settings.LIVE_RECORDING_S3_SECRET,
        )
    )


def upload_target() -> dict:
    """Where LiveKit uploads a recording (its S3Upload)."""
    return {
        "access_key": settings.LIVE_RECORDING_S3_ACCESS_KEY,
        "secret": settings.LIVE_RECORDING_S3_SECRET,
        "region": settings.LIVE_RECORDING_S3_REGION,
        "endpoint": settings.LIVE_RECORDING_S3_ENDPOINT,
        "bucket": settings.LIVE_RECORDING_S3_BUCKET,
        "force_path_style": settings.LIVE_RECORDING_S3_FORCE_PATH_STYLE,
    }


def _client():
    style = "path" if settings.LIVE_RECORDING_S3_FORCE_PATH_STYLE else "auto"
    return boto3.client(
        "s3",
        endpoint_url=settings.LIVE_RECORDING_S3_ENDPOINT,
        region_name=settings.LIVE_RECORDING_S3_REGION,
        aws_access_key_id=settings.LIVE_RECORDING_S3_ACCESS_KEY,
        aws_secret_access_key=settings.LIVE_RECORDING_S3_SECRET,
        config=Config(s3={"addressing_style": style}),
    )


@contextmanager
def download(key: str) -> Iterator:
    """The recording at `key`, in a temporary file that goes when done."""
    with tempfile.TemporaryFile() as handle:
        _client().download_fileobj(settings.LIVE_RECORDING_S3_BUCKET, key, handle)
        handle.seek(0)
        yield handle


def delete(key: str) -> None:
    _client().delete_object(Bucket=settings.LIVE_RECORDING_S3_BUCKET, Key=key)
