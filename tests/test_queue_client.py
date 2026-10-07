import base64
import json

import pytest

from backend.queue_client import QueueClient, decode_queue_body, validate_r2_event


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def make_event(action: str = "PutObject", account: str = "account", bucket: str = "source") -> dict:
    return {
        "account": account,
        "action": action,
        "bucket": bucket,
        "object": {"key": "archives/item.tar.gz", "size": 12, "eTag": "etag-1"},
    }


def test_queue_pull_decodes_nested_messages_and_cf_content_type(monkeypatch) -> None:
    body = {"account": "account", "action": "PutObject", "bucket": "source"}
    encoded = base64.b64encode(json.dumps(body).encode("utf-8")).decode("ascii")
    captured: list[tuple[str, dict]] = []

    def fake_urlopen(request, timeout):
        captured.append((request.full_url, json.loads(request.data.decode("utf-8"))))
        return FakeResponse(
            {
                "success": True,
                "result": {
                    "message_backlog_count": 42,
                    "messages": [
                        {
                            "lease_id": "lease-1",
                            "body": encoded,
                            "metadata": {"CF-Content-Type": "json"},
                        }
                    ]
                },
            }
        )

    monkeypatch.setattr("backend.queue_client.urllib_request.urlopen", fake_urlopen)
    queue = QueueClient("account", "queue", "token")
    messages = queue.pull(60000, 10)

    assert messages == [type(messages[0])("lease-1", body)]
    assert queue.last_backlog_count == 42
    assert captured[0][1] == {"visibility_timeout_ms": 60000, "batch_size": 10}


def test_queue_pull_ack_uses_cloudflare_ack_envelope(monkeypatch) -> None:
    payloads: list[dict] = []

    def fake_urlopen(request, timeout):
        payloads.append(json.loads(request.data.decode("utf-8")))
        return FakeResponse({"success": True, "result": {}})

    monkeypatch.setattr("backend.queue_client.urllib_request.urlopen", fake_urlopen)
    QueueClient("account", "queue", "token").ack(["lease-1", "lease-2"])

    assert payloads == [{"acks": [{"lease_id": "lease-1"}, {"lease_id": "lease-2"}], "retries": []}]


@pytest.mark.parametrize("action", ["PutObject", "CopyObject", "CompleteMultipartUpload"])
def test_validate_r2_event_accepts_all_supported_create_actions(action: str) -> None:
    validated = validate_r2_event(make_event(action), "account")
    assert validated is not None
    assert validated[3] == action


@pytest.mark.parametrize(
    "event",
    [
        make_event(account="other-account"),
        make_event(bucket=""),
        {**make_event(), "object": {"key": "x", "size": -1}},
        {**make_event(), "action": "DeleteObject"},
        {**make_event(), "object": {"key": "x", "size": 1, "eTag": []}},
    ],
)
def test_validate_r2_event_rejects_malformed_or_wrong_account(event: dict) -> None:
    assert validate_r2_event(event, "account") is None


def test_validate_r2_event_enforces_configured_source_bucket_allowlist() -> None:
    assert validate_r2_event(make_event(bucket="source"), "account", {"source"}) is not None
    assert validate_r2_event(make_event(bucket="other"), "account", {"source"}) is None


def test_decode_queue_body_rejects_invalid_base64() -> None:
    assert decode_queue_body("not-base64", {"CF-Content-Type": "json"}) is None
