"""R2 client abstractions for scanning, preflight, and explicit copies."""

from collections.abc import Iterator
from typing import BinaryIO, Protocol, cast

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from .models import ConnectionSettings, SourceObject


class R2Client(Protocol):
    """The R2 operations required by scanning, preflight, and synchronization."""

    def test_source_bucket(self, bucket_name: str) -> None:
        """Validate that a known source bucket can be accessed."""

    def list_source_objects(self, bucket_name: str) -> Iterator[SourceObject]:
        """Yield source object metadata from a known bucket."""

    def open_object(self, bucket_name: str, object_key: str) -> BinaryIO:
        """Open one source object as a streaming binary file."""

    def object_exists(self, bucket_name: str, object_key: str) -> bool:
        """Check whether a destination object already exists."""

    def copy_object(
        self,
        source_bucket: str,
        source_key: str,
        target_bucket: str,
        target_key: str,
    ) -> None:
        """Copy one complete source object to a target bucket."""


class Boto3R2Client:
    """S3-compatible client for source reads and explicit server-side copies."""

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

    def object_exists(self, bucket_name: str, object_key: str) -> bool:
        try:
            self._client.head_object(Bucket=bucket_name, Key=object_key)
        except ClientError as error:
            error_code = str(error.response.get("Error", {}).get("Code", ""))
            status_code = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if error_code in {"404", "NoSuchKey", "NotFound"} or status_code == 404:
                return False
            raise
        return True

    def copy_object(
        self,
        source_bucket: str,
        source_key: str,
        target_bucket: str,
        target_key: str,
    ) -> None:
        self._client.copy_object(
            Bucket=target_bucket,
            Key=target_key,
            CopySource={"Bucket": source_bucket, "Key": source_key},
        )


def create_r2_client(connection_settings: ConnectionSettings) -> R2Client:
    """Create the R2 client used by scanning, preflight, and explicit sync."""

    return Boto3R2Client(connection_settings)
