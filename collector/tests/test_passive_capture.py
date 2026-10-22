"""Submitted sanitized JSON replaces the raw HAR dependency. No real hotel traffic."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

import passive_capture as pc

FIXTURE = Path(__file__).parent / "fixtures" / "marriott_cn_NYCTE_sanitized.json"
URL = "https://www.marriott.com.cn/mi/query/PhoenixBookDTTSearchProductsByProperty"


@pytest.fixture
def sample():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def process(sample):
    return pc.process_capture(
        URL,
        json.dumps({"operationName": sample["operation"], "variables": sample["variables"]}),
        json.dumps(sample["response"]),
    )


@pytest.mark.parametrize("site", ["www.marriott.com", "www.marriott.com.cn"])
def test_exact_official_sources(site):
    assert pc.is_target("https://" + site + next(iter(pc.TARGET_PATHS)))


@pytest.mark.parametrize(
    "url",
    [
        "https://evilmarriott.com/mi/query/PhoenixBookDTTSearchProductsByProperty",
        "https://www.marriott.com.evil.invalid/mi/query/PhoenixBookDTTSearchProductsByProperty",
        "http://www.marriott.com/mi/query/PhoenixBookDTTSearchProductsByProperty",
        "https://user:secret@www.marriott.com/mi/query/PhoenixBookDTTSearchProductsByProperty",
        "https://www.marriott.com:8443/mi/query/PhoenixBookDTTSearchProductsByProperty",
        "https://www.marriott.com/mi/phoenix-gateway/session",
    ],
)
def test_non_target_sources_rejected(url):
    assert not pc.is_target(url)
    assert not pc.is_target(URL, "GET")


def test_sensitive_nested_containers_and_response_are_redacted(sample):
    sample["variables"]["search"]["options"]["customerId"] = "synthetic-customer-value"
    sample["variables"]["nested"] = {"authorization": {"value": "opaque-secret"}, "cookie": ["opaque-secret"]}
    sample["response"]["data"]["testMetadata"] = {
        "customerId": "opaque-secret",
        "creditCard": {"number": "test-not-a-real-card"},
    }
    record = process(sample)
    text = json.dumps(record)
    assert (
        "synthetic-customer-value" not in text
        and "opaque-secret" not in text
        and "test-not-a-real-card" not in text
    )
    assert record["variables"]["nested"]["authorization"] == "REDACTED"
    assert record["response"]["data"]["testMetadata"]["customerId"] == "REDACTED"


@pytest.mark.parametrize(
    "value", ["foo@example.invalid", "eyJhbGciOiJIUzI1NiIsInR5cCI6.eyJzdWIiOiIxMjM0NTY3ODkwIn0"]
)
def test_unknown_field_with_email_or_jwt_refuses_write(sample, value):
    sample["response"]["data"]["unknown"] = value
    with pytest.raises(ValueError, match="拒绝"):
        process(sample)


def test_partial_graphql_error_not_accepted(sample):
    sample["response"]["errors"] = [{"message": "partial failure"}]
    with pytest.raises(ValueError):
        process(sample)


def test_real_sanitized_sample_and_content_dedup(tmp_path, sample):
    record = process(sample)
    assert record["source_site"] == "www.marriott.com.cn" and record["property_id"] == "NYCTE"
    assert record["variables"]["search"]["options"]["customerId"] == "REDACTED"
    assert len(record["response"]["data"]["commerce"]["product"]["searchProductsByProperty"]["edges"]) == 75
    seen = set()
    first = pc.save_record(record, tmp_path, seen)
    assert first and first.exists() and "off0" in first.name
    assert pc.save_record(record, tmp_path, seen) is None
    written = json.loads(first.read_text(encoding="utf-8"))
    assert written["captured_at"] == record["captured_at"]
    other = deepcopy(record)
    other["source_site"] = "www.marriott.com"
    assert pc.content_hash(other) != pc.content_hash(record)


@pytest.mark.parametrize("property_id,offset", [("../escape", 0), ("NYCTE", "../../escape"), ("NYCTE", -1)])
def test_filename_inputs_are_not_paths(sample, property_id, offset):
    sample["variables"]["search"]["propertyId"] = property_id
    sample["variables"]["offset"] = offset
    with pytest.raises(ValueError):
        process(sample)


async def test_non_200_never_requests_response_or_retries(tmp_path):
    class NoSend:
        async def send(self, message):
            raise AssertionError("No CDP body lookup is needed for denied responses")

    collector = pc.Collector(NoSend(), tmp_path)
    await collector.handle_finished("s", "r", {"url": URL, "post": None, "status": 403})
    assert collector.count == 0 and not list(tmp_path.glob("*.json"))
    await collector.close()


async def test_redirect_request_id_cannot_import_another_origin(tmp_path):
    class NoSend:
        async def send(self, message):
            raise AssertionError("Redirected response must never be read")

    collector = pc.Collector(NoSend(), tmp_path)
    base = {"sessionId": "session"}
    await collector.on_event(
        {
            **base,
            "method": "Network.requestWillBeSent",
            "params": {"requestId": "request", "request": {"url": URL, "method": "POST", "postData": "{}"}},
        }
    )
    assert collector.pending
    await collector.on_event(
        {
            **base,
            "method": "Network.requestWillBeSent",
            "params": {
                "requestId": "request",
                "request": {"url": "https://example.invalid/redirect", "method": "GET"},
            },
        }
    )
    await collector.on_event(
        {**base, "method": "Network.loadingFinished", "params": {"requestId": "request"}}
    )
    assert not collector.pending and collector.count == 0
    await collector.close()


async def test_malformed_input_error_does_not_echo_private_values(tmp_path, sample, capsys):
    sample["variables"]["search"]["options"]["startDate"] = "synthetic.private@example.invalid"
    post = json.dumps({"operationName": sample["operation"], "variables": sample["variables"]})
    collector = pc.Collector(None, tmp_path)

    async def body(*args, **kwargs):
        return {"body": json.dumps(sample["response"]), "base64Encoded": False}

    collector.send = body
    await collector.handle_finished("session", "request", {"url": URL, "post": post, "status": 200})
    assert "synthetic.private" not in capsys.readouterr().out
    assert collector.count == 0
    await collector.close()
