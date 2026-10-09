"""S3-compatible R2 access and Cloudflare Queue HTTP pull primitives."""

from collections.abc import Iterator
import threading
from typing import Any, BinaryIO, Protocol, cast

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from .models import ConnectionSettings, SourceObject
from .transfer import (
    Boto3TransferAdapter,
    ObjectTransferAdapter,
    RcloneTransferAdapter,
    TransferOutcome,
    TargetVerificationError,
    TransferRequest,
)


class R2Client(Protocol):
    def test_source_bucket(self, bucket_name: str) -> None: ...
    def list_source_objects(self, bucket_name: str) -> Iterator[SourceObject]: ...
    def open_object(self, bucket_name: str, object_key: str) -> BinaryIO: ...
    def open_object_conditional(self, bucket_name: str, object_key: str, etag: str | None = None) -> BinaryIO: ...
    def head_object(self, bucket_name: str, object_key: str) -> SourceObject: ...
    def object_exists(self, bucket_name: str, object_key: str) -> bool: ...
    def copy_object(
        self,
        source_bucket: str,
        source_key: str,
        target_bucket: str,
        target_key: str,
    ) -> None: ...
    def transfer_object(
        self,
        request: TransferRequest,
        cancellation_event: threading.Event | None = None,
    ) -> TransferOutcome: ...


class Boto3R2Client:
    """R2 client. Copy remains server-side and target operations never overwrite."""

    def __init__(self, connection_settings: ConnectionSettings) -> None:
        self._client = boto3.client(
            "s3",
            endpoint_url=connection_settings.endpoint,
            aws_access_key_id=connection_settings.access_key_id,
            aws_secret_access_key=connection_settings.secret_access_key,
            region_name="auto",
            config=Config(connect_timeout=120, read_timeout=120, retries={"max_attempts": 0}),
        )
        self._transfer_adapter = self._create_transfer_adapter(connection_settings)

    def _create_transfer_adapter(
        self, connection_settings: ConnectionSettings
    ) -> ObjectTransferAdapter:
        if connection_settings.transfer_adapter == "boto3":
            return Boto3TransferAdapter(self._client)
        if connection_settings.transfer_adapter == "rclone":
            return RcloneTransferAdapter(
                binary_path=connection_settings.rclone_binary_path,
                endpoint=connection_settings.endpoint,
                access_key_id=connection_settings.access_key_id,
                secret_access_key=connection_settings.secret_access_key,
                source_bucket=connection_settings.source_bucket,
                timeout_seconds=connection_settings.rclone_transfer_timeout_seconds,
                output_limit_bytes=connection_settings.rclone_output_limit_bytes,
                low_level_retries=connection_settings.rclone_low_level_retries,
            )
        raise ValueError("Unsupported R2 transfer adapter")

    def test_source_bucket(self, bucket_name: str) -> None:
        self._client.head_bucket(Bucket=bucket_name)

    def _source_object_from_raw(self, raw_object: dict[str, Any]) -> SourceObject | None:
        object_key = raw_object.get("Key")
        object_size = raw_object.get("Size")
        if not isinstance(object_key, str) or not isinstance(object_size, int):
            return None
        etag = raw_object.get("ETag")
        last_modified = raw_object.get("LastModified")
        return SourceObject(
            key=object_key,
            size=object_size,
            etag=str(etag) if etag is not None else None,
            last_modified=last_modified.isoformat() if hasattr(last_modified, "isoformat") else (str(last_modified) if last_modified else None),
        )

    def list_source_objects(self, bucket_name: str) -> Iterator[SourceObject]:
        paginator = self._client.get_paginator("list_objects_v2")
        for response in paginator.paginate(Bucket=bucket_name):
            response_contents = response.get("Contents", [])
            if not isinstance(response_contents, list):
                continue
            for raw_object in response_contents:
                if isinstance(raw_object, dict):
                    source_object = self._source_object_from_raw(raw_object)
                    if source_object is not None:
                        yield source_object

    def list_source_page(self, bucket_name: str, continuation_token: str | None = None, page_size: int = 1000) -> tuple[list[SourceObject], str | None]:
        parameters: dict[str, Any] = {"Bucket": bucket_name, "MaxKeys": page_size}
        if continuation_token:
            parameters["ContinuationToken"] = continuation_token
        response = self._client.list_objects_v2(**parameters)
        raw_contents = response.get("Contents", [])
        objects = [
            parsed
            for raw in raw_contents
            if isinstance(raw, dict) and (parsed := self._source_object_from_raw(raw)) is not None
        ]
        next_token = response.get("NextContinuationToken") if response.get("IsTruncated") else None
        return objects, str(next_token) if next_token else None

    def head_object(self, bucket_name: str, object_key: str) -> SourceObject:
        response = self._client.head_object(Bucket=bucket_name, Key=object_key)
        etag = response.get("ETag")
        last_modified = response.get("LastModified")
        return SourceObject(
            key=object_key,
            size=int(response.get("ContentLength", 0)),
            etag=str(etag) if etag is not None else None,
            last_modified=last_modified.isoformat() if hasattr(last_modified, "isoformat") else (str(last_modified) if last_modified else None),
        )

    def open_object(self, bucket_name: str, object_key: str) -> BinaryIO:
        return self.open_object_conditional(bucket_name, object_key)

    def open_object_conditional(self, bucket_name: str, object_key: str, etag: str | None = None) -> BinaryIO:
        parameters: dict[str, Any] = {"Bucket": bucket_name, "Key": object_key}
        if etag:
            parameters["IfMatch"] = etag
        response = self._client.get_object(**parameters)
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
        """Retain the original boto3 CopyObject contract for compatibility."""
        self._client.copy_object(
            Bucket=target_bucket,
            Key=target_key,
            CopySource={"Bucket": source_bucket, "Key": source_key},
        )

    def transfer_object(
        self,
        request: TransferRequest,
        cancellation_event: threading.Event | None = None,
    ) -> TransferOutcome:
        outcome = self._transfer_adapter.transfer_object(request, cancellation_event)
        if not self._transfer_adapter.requires_target_verification:
            return outcome
        target = self.head_object(request.target_bucket, request.target_key)
        if target.size != request.expected_size:
            raise TargetVerificationError("Transferred object size verification failed")
        return outcome


def create_r2_client(connection_settings: ConnectionSettings) -> R2Client:
    return Boto3R2Client(connection_settings)
