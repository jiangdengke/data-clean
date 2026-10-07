"""Cloudflare Queues HTTP pull consumer helpers with strict event validation."""

import base64
import binascii
import json
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any
from urllib import request as urllib_request


@dataclass(frozen=True)
class QueueMessage:
    lease_id: str
    body: dict[str, Any]


class QueueClient:
    """Small REST client for Cloudflare Queue HTTP pull/ack endpoints."""

    def __init__(self, account_id: str, queue_id: str, api_token: str, timeout: int = 30) -> None:
        self.base_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/queues/{queue_id}"
        self.api_token = api_token
        self.timeout = timeout
        self.last_backlog_count: int | None = None

    def _request(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib_request.Request(
            f"{self.base_url}{path}",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib_request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except Exception as error:
            raise QueueTransportError from error
        if not isinstance(result, dict) or result.get("success") is False:
            raise QueueTransportError
        return result

    def pull(self, visibility_timeout_ms: int, batch_size: int) -> list[QueueMessage]:
        result = self._request(
            "/messages/pull",
            {"visibility_timeout_ms": visibility_timeout_ms, "batch_size": batch_size},
        )
        # The documented REST response wraps messages in result.messages.
        # Keep accepting a list here for small mock clients and older fixtures.
        result_payload = result.get("result", {})
        if isinstance(result_payload, dict):
            backlog_count = result_payload.get("message_backlog_count")
            if isinstance(backlog_count, int) and not isinstance(backlog_count, bool) and backlog_count >= 0:
                self.last_backlog_count = backlog_count
            raw_messages = result_payload.get("messages", [])
        else:
            raw_messages = result_payload
        if not isinstance(raw_messages, list):
            return []
        messages: list[QueueMessage] = []
        for raw_message in raw_messages:
            if not isinstance(raw_message, dict):
                continue
            lease_id = raw_message.get("lease_id")
            if not isinstance(lease_id, str) or not lease_id:
                continue
            metadata = raw_message.get("metadata")
            body = decode_queue_body(raw_message.get("body"), metadata if isinstance(metadata, dict) else raw_message)
            if body is not None:
                messages.append(QueueMessage(lease_id=lease_id, body=body))
        return messages

    def ack(self, lease_ids: list[str]) -> None:
        if not lease_ids:
            return
        self._request("/messages/ack", {"acks": [{"lease_id": item} for item in lease_ids], "retries": []})


class QueueTransportError(RuntimeError):
    """Provider error; intentionally does not retain provider response data."""


def decode_queue_body(body: Any, metadata: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Decode a pull message using Cloudflare's content-type encoding rules.

    Queue pull responses put the content type in ``metadata["CF-Content-Type"]``.
    JSON and bytes messages are RFC 4648 base64 encoded; text messages are plain
    UTF-8.  A dict body is retained for tests and local adapters that already
    decoded the payload.
    """

    value: Any = body
    message_metadata = metadata or {}
    content_type = str(
        message_metadata.get("CF-Content-Type", message_metadata.get("content_type", ""))
    ).lower()
    encoded = content_type in {"json", "bytes"}
    encoded = encoded or bool(message_metadata.get("base64"))
    encoded = encoded or str(message_metadata.get("encoding", "")).lower() == "base64"
    if isinstance(value, dict) and "data" in value:
        encoded = encoded or str(value.get("encoding", "")).lower() == "base64"
        value = value.get("data")
    if isinstance(value, str) and encoded:
        try:
            value = base64.b64decode(value, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return None
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(value, str):
        if "json" not in content_type and not value.lstrip().startswith("{"):
            return None
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def sanitize_r2_event(event: dict[str, Any]) -> dict[str, Any]:
    """Return only the documented, non-secret R2 event fields."""

    safe_event: dict[str, Any] = {}
    for field_name in ("account", "action", "bucket"):
        value = event.get(field_name)
        if isinstance(value, str):
            safe_event[field_name] = value
    event_time = event.get("eventTime")
    if isinstance(event_time, str):
        safe_event["eventTime"] = event_time

    raw_object = event.get("object")
    if isinstance(raw_object, dict):
        safe_object: dict[str, Any] = {}
        object_key = raw_object.get("key")
        object_size = raw_object.get("size")
        object_etag = raw_object.get("eTag", raw_object.get("etag"))
        if isinstance(object_key, str):
            safe_object["key"] = object_key
        if isinstance(object_size, int) and not isinstance(object_size, bool):
            safe_object["size"] = object_size
        if isinstance(object_etag, str):
            safe_object["eTag"] = object_etag
        if safe_object:
            safe_event["object"] = safe_object

    raw_copy_source = event.get("copySource")
    if isinstance(raw_copy_source, dict):
        safe_copy_source: dict[str, Any] = {}
        for field_name in ("bucket", "key", "versionId", "eTag", "etag"):
            value = raw_copy_source.get(field_name)
            if isinstance(value, str):
                safe_copy_source["eTag" if field_name == "etag" else field_name] = value
        copy_size = raw_copy_source.get("size")
        if isinstance(copy_size, int) and not isinstance(copy_size, bool):
            safe_copy_source["size"] = copy_size
        if safe_copy_source:
            safe_event["copySource"] = safe_copy_source
    return safe_event


def validate_r2_event(
    event: dict[str, Any],
    expected_account: str | None = None,
    expected_buckets: Collection[str] | None = None,
) -> tuple[str, str, str, str, int, str | None] | None:
    """Return safe event fields only when action and configured source are valid."""

    account = event.get("account")
    action = event.get("action")
    bucket = event.get("bucket")
    obj = event.get("object")
    if expected_account and account != expected_account:
        return None
    if not isinstance(account, str) or not isinstance(action, str) or action not in {
        "PutObject",
        "CopyObject",
        "CompleteMultipartUpload",
    }:
        return None
    if not isinstance(bucket, str) or not bucket or not isinstance(obj, dict):
        return None
    if expected_buckets is not None and bucket not in expected_buckets:
        return None
    key = obj.get("key")
    size = obj.get("size")
    etag = obj.get("eTag", obj.get("etag"))
    if not isinstance(key, str) or not key or not isinstance(size, int) or size < 0:
        return None
    if etag is not None and not isinstance(etag, str):
        return None
    return account, bucket, key, action, size, etag
