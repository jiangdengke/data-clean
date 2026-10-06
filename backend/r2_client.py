"""Read-only R2 client abstractions and the boto3 implementation."""

from collections.abc import Iterator
from typing import BinaryIO, Protocol, cast

import boto3
from botocore.client import Config

from .models import ConnectionSettings, SourceObject


class ReadOnlyR2Client(Protocol):
    """The source operations required by a scan."""

    def test_source_bucket(self, bucket_name: str) -> None:
        """Validate that a known source bucket can be accessed."""

    def list_source_objects(self, bucket_name: str) -> Iterator[SourceObject]:
        """Yield source object metadata from a known bucket."""

    def open_object(self, bucket_name: str, object_key: str) -> BinaryIO:
        """Open one source object as a streaming binary file."""


class Boto3ReadOnlyR2Client:
    """S3-compatible client limited to the application's read-only calls."""

    def __init__(self, connection_settings: ConnectionSettings) -> None:
        self._client = boto3.client(
            "s3",
            endpoint_url=connection_settings.endpoint,
            aws_access_key_id=connection_settings.access_key_id,
            aws_secret_access_key=connection_settings.secret_access_key,
            region_name="auto",
            config=Config(
                connect_timeout=120,
                read_timeout=120,
                retries={"max_attempts": 0},
            ),
        )

    def test_source_bucket(self, bucket_name: str) -> None:
        self._client.head_bucket(Bucket=bucket_name)

    def list_source_objects(self, bucket_name: str) -> Iterator[SourceObject]:
        paginator = self._client.get_paginator("list_objects_v2")
        listed_objects: list[SourceObject] = []
        for response in paginator.paginate(Bucket=bucket_name):
            response_contents = response.get("Contents", [])
            if not isinstance(response_contents, list):
                continue
            for raw_object in response_contents:
                if not isinstance(raw_object, dict):
                    continue
                object_key = raw_object.get("Key")
                object_size = raw_object.get("Size")
                if isinstance(object_key, str) and isinstance(object_size, int):
                    listed_objects.append(SourceObject(key=object_key, size=object_size))

        for source_object in sorted(listed_objects, key=lambda item: item.key):
            yield source_object

    def open_object(self, bucket_name: str, object_key: str) -> BinaryIO:
        response = self._client.get_object(Bucket=bucket_name, Key=object_key)
        response_body = response.get("Body")
        if response_body is None:
            raise RuntimeError("R2 returned an empty object body")
        return cast(BinaryIO, response_body)


def create_r2_client(connection_settings: ConnectionSettings) -> ReadOnlyR2Client:
    """Create the only R2 client used by this read-only application slice."""

    return Boto3ReadOnlyR2Client(connection_settings)
