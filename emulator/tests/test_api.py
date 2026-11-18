"""Test 3 (API contract) and receiver-side guarantees: idempotency, stale versions, chaos."""

from __future__ import annotations

import copy
import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import SYNC_PATH, create_app


def make_payload(**overrides) -> dict:
    payload = {
        "schema_version": "1.0",
        "message_id": str(uuid.uuid4()),
        "source": "1c-fitness",
        "operation": "upsert",
        "version": 1_763_450_000_000,
        "object_type": "ПродажаАбонемента",
        "document_id": str(uuid.uuid4()),
        "document_number": "ФК-000123",
        "document_date": "2026-11-18T10:15:00",
        "client_id": str(uuid.uuid4()),
        "client_name": "Иванова Анна",
        "amount": 45000,
        "currency": "KZT",
        "items": [
            {"line": 1, "name": "Абонемент «Безлимит 3 мес.»", "quantity": 1, "price": 40000, "amount": 40000},
            {"line": 2, "name": "Персональная тренировка", "quantity": 1, "price": 5000, "amount": 5000},
        ],
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(failure_rate=0.0, seed=1))


def post(client: TestClient, payload: dict, key: str | None = None):
    headers = {"Idempotency-Key": key or payload.get("message_id") or str(uuid.uuid4())}
    return client.post(SYNC_PATH, json=payload, headers=headers)


# --- Happy path -------------------------------------------------------------------------

def test_valid_document_is_accepted(client):
    payload = make_payload()
    response = post(client, payload)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert body["document_id"] == payload["document_id"]
    assert body["accounting_entry_id"].startswith("ACC-")


def test_payload_as_1c_serializes_it(client):
    """1C writes numbers like 45000 and 1.5 and may add fields the receiver does not know."""
    payload = make_payload(amount=4500.5, extra_field_from_newer_1c="ignored")
    payload["items"] = [{"name": "Разовое посещение", "quantity": 1.5, "price": 3000.33, "amount": 4500.5}]
    assert post(client, payload).status_code == 200


# --- Test 3: contract validation --------------------------------------------------------

@pytest.mark.parametrize("field", ["document_id", "client_name", "amount", "items", "object_type"])
def test_missing_required_field_is_rejected(client, field):
    payload = make_payload()
    del payload[field]

    response = post(client, payload)

    assert response.status_code == 400
    body = response.json()
    assert body["status"] == "rejected"
    assert any(err["field"] == field for err in body["detail"])


@pytest.mark.parametrize(
    "mutation, bad_field",
    [
        (lambda p: p.update(document_id="not-a-uuid"), "document_id"),
        (lambda p: p.update(client_name="   "), "client_name"),
        (lambda p: p.update(amount=0), "amount"),
        (lambda p: p.update(amount=-100), "amount"),
        (lambda p: p.update(amount="сорок тысяч"), "amount"),
        (lambda p: p.update(items=[]), "items"),
        (lambda p: p.update(items="абонемент"), "items"),
        (lambda p: p.update(object_type="Подарок"), "object_type"),
        (lambda p: p.update(currency="тенге"), "currency"),
        (lambda p: p.update(operation="delete"), "operation"),
        (lambda p: p["items"][0].pop("name"), "items.0.name"),
        (lambda p: p["items"][0].update(quantity=0), "items.0.quantity"),
        (lambda p: p["items"][1].update(price=-1), "items.1.price"),
    ],
)
def test_invalid_field_is_rejected(client, mutation, bad_field):
    payload = make_payload()
    mutation(payload)

    response = post(client, payload)

    assert response.status_code == 400, response.text
    assert any(err["field"] == bad_field for err in response.json()["detail"])


def test_amount_must_match_items_total(client):
    response = post(client, make_payload(amount=50000))  # items sum to 45000

    assert response.status_code == 400
    assert "does not match" in response.text


def test_malformed_json_is_rejected(client):
    response = client.post(
        SYNC_PATH,
        content=b'{"document_id": "abc", ',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["status"] == "rejected"


def test_rejected_packet_is_not_stored(client):
    post(client, make_payload(items=[]))
    assert client.get("/api/v1/accounting/documents").json() == []


def test_validate_endpoint_is_a_dry_run(client):
    assert client.post("/api/v1/accounting/validate", json=make_payload()).status_code == 200
    assert client.post("/api/v1/accounting/validate", json=make_payload(items=[])).status_code == 400
    assert client.get("/api/v1/accounting/documents").json() == []


# --- Delivery guarantees ----------------------------------------------------------------

def test_redelivery_with_same_idempotency_key_is_deduplicated(client):
    """At-least-once from 1C + idempotent receiver = effectively exactly-once."""
    payload = make_payload()

    first = post(client, payload, key="msg-1").json()
    second = post(client, payload, key="msg-1").json()

    assert first["status"] == "accepted"
    assert second["status"] == "duplicate"
    assert first["accounting_entry_id"] == second["accounting_entry_id"]
    assert len(client.get("/api/v1/accounting/documents").json()) == 1


def test_new_version_of_document_updates_same_entry(client):
    v1 = make_payload(version=100)
    v2 = copy.deepcopy(v1) | {"message_id": str(uuid.uuid4()), "version": 200}

    first = post(client, v1).json()
    second = post(client, v2).json()

    assert second["status"] == "updated"
    assert second["accounting_entry_id"] == first["accounting_entry_id"]


def test_stale_version_does_not_overwrite_newer_one(client):
    """An old snapshot stuck in retries must not overwrite a newer one."""
    newer = make_payload(version=200, client_name="Новое имя")
    older = copy.deepcopy(newer) | {"message_id": str(uuid.uuid4()), "version": 100, "client_name": "Старое имя"}

    post(client, newer)
    response = post(client, older).json()

    assert response["status"] == "stale_ignored"
    stored = client.get(f"/api/v1/accounting/documents/{newer['document_id']}").json()
    assert stored["client_name"] == "Новое имя"


def test_cancel_operation(client):
    doc = make_payload(version=100)
    post(client, doc)
    cancel = copy.deepcopy(doc) | {"message_id": str(uuid.uuid4()), "version": 200, "operation": "cancel"}

    assert post(client, cancel).json()["status"] == "cancelled"


# --- Chaos (input for Test 2) -----------------------------------------------------------

def test_failure_rate_one_always_returns_500():
    client = TestClient(create_app(failure_rate=1.0, seed=1))
    response = post(client, make_payload())

    assert response.status_code == 500
    assert response.json() == {"status": "error", "error": "internal_error", "detail": "Simulated failure"}
    assert client.get("/api/v1/accounting/documents").json() == []


def test_failure_rate_is_roughly_twenty_percent():
    client = TestClient(create_app(failure_rate=0.2, seed=42))
    codes = [post(client, make_payload()).status_code for _ in range(500)]
    share = codes.count(500) / len(codes)

    assert 0.15 <= share <= 0.25, share


def test_validation_runs_before_chaos():
    """A broken packet is always 400, never a random 500: 1C must not retry it forever."""
    client = TestClient(create_app(failure_rate=1.0, seed=1))
    assert post(client, make_payload(items=[])).status_code == 400


def test_chaos_can_be_changed_at_runtime(client):
    assert client.put("/api/v1/admin/chaos", json={"failure_rate": 1.0}).status_code == 200
    assert post(client, make_payload()).status_code == 500
    client.put("/api/v1/admin/chaos", json={"failure_rate": 0.0})
    assert post(client, make_payload()).status_code == 200
