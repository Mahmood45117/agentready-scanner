"""
Spec-conformance tests for scanner/acp.py (Agentic Checkout, API-Version 2025-09-12).
See tests/conftest.py for the fake WooCommerce store and fixtures. No network, no sleeps.
"""
import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest

import acp
from tests.conftest import ADDRESS, BUYER, MERCHANT_SLUG, Api, db_rows

SPEC_TOTAL_TYPES = ["items_base_amount", "items_discount", "subtotal", "discount", "fulfillment", "tax", "fee", "total"]


def _rfc3339(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def totals_map(body):
    return {t["type"]: t["amount"] for t in body["totals"]}


# ============================================================================ 1. create
def test_create_session_returns_spec_shaped_session(api, store):
    st, body, hdrs = api.create({"items": [{"id": "90", "quantity": 2}]}, key="idem_create_1", Request_Id="req_abc")
    assert st == 201, body
    assert body["id"].startswith("cs_")
    assert body["status"] == "not_ready_for_payment"
    assert body["currency"] == "usd"
    assert body["payment_provider"] == {"provider": "stripe", "supported_payment_methods": ["card"]}

    # line_items math: base = unit*qty, subtotal = base - discount, total = subtotal + tax
    [li] = body["line_items"]
    assert li["item"] == {"id": "90", "quantity": 2}
    assert li["base_amount"] == 3900 * 2
    assert li["discount"] == 0
    assert li["subtotal"] == li["base_amount"] - li["discount"]
    assert li["tax"] == 0
    assert li["total"] == li["subtotal"] + li["tax"]

    # totals: all 8 spec types, in spec order, consistent with line items
    assert [t["type"] for t in body["totals"]] == SPEC_TOTAL_TYPES
    assert all(isinstance(t["amount"], int) and t.get("display_text") for t in body["totals"])
    tm = totals_map(body)
    assert tm["items_base_amount"] == 7800 and tm["subtotal"] == 7800 and tm["total"] == 7800
    assert tm["fulfillment"] == 0   # no address yet -> nothing selected

    # fulfillment options carry carrier and RFC 3339 delivery window from the merchant shipping map
    [opt] = body["fulfillment_options"]
    assert opt["type"] == "shipping" and opt["id"] == "flat_rate:1" and opt["title"] == "Flat rate"
    assert opt["carrier"] == "USPS"
    assert opt["subtotal"] == 690 and opt["tax"] == 0 and opt["total"] == 690
    for k in ("earliest_delivery_time", "latest_delivery_time"):
        assert opt[k].endswith("Z")
        datetime.fromisoformat(opt[k].replace("Z", "+00:00"))
    earliest = datetime.fromisoformat(opt["earliest_delivery_time"].replace("Z", "+00:00"))
    latest = datetime.fromisoformat(opt["latest_delivery_time"].replace("Z", "+00:00"))
    assert timedelta(days=3) <= earliest - datetime.now(timezone.utc) + timedelta(seconds=5)
    assert latest - earliest == timedelta(days=4)
    assert not any(k.startswith("_") for k in opt), "internal fields must not leak"
    assert body["fulfillment_option_id"] is None
    assert body["messages"] == []

    links = {l["type"]: l["url"] for l in body["links"]}
    assert links["terms_of_use"] == "https://store.test/terms/"
    assert links["privacy_policy"] == "https://store.test/privacy/"

    assert hdrs["Idempotency-Key"] == "idem_create_1"
    assert hdrs["Request-Id"] == "req_abc"
    assert hdrs["API-Version"] == acp.API_VERSION
    assert hdrs["Content-Type"].startswith("application/json")


def test_create_with_address_and_buyer_is_ready_for_payment(api):
    st, body, _ = api.create({"items": [{"id": "90", "quantity": 1}], "fulfillment_address": ADDRESS, "buyer": BUYER})
    assert st == 201
    assert body["status"] == "ready_for_payment"
    assert body["fulfillment_option_id"] == "flat_rate:1"
    assert body["fulfillment_address"] == ADDRESS and body["buyer"] == BUYER
    assert totals_map(body)["total"] == 4590


# ============================================================================ 2. auth / headers
def test_missing_bearer_is_401_with_error_body(api):
    st, body, _ = api.create(Authorization=None)
    assert st == 401
    assert body["type"] == "invalid_request" and body["code"] and body["message"]
    assert set(body) >= {"type", "code", "message"}


def test_wrong_bearer_is_401(api):
    st, body, _ = api.create(Authorization="Bearer nope")
    assert st == 401 and body["code"] == "unauthorized"


def test_unknown_merchant_is_indistinguishable_from_bad_bearer(client):
    # 401 (not 404) so merchant slugs can't be enumerated
    r = client.post("/acp/nope/checkout_sessions", headers={"Authorization": "Bearer test-bearer"}, data="{}")
    assert r.status_code == 401 and r.get_json()["code"] == "unauthorized"


def test_wrong_api_version_is_400_with_param(api):
    st, body, _ = api.create(API_Version="2024-01-01")
    assert st == 400
    assert body["code"] == "invalid" and body["param"] == "$.headers.API-Version"


def test_stale_timestamp_is_400(api):
    old = _rfc3339(datetime.now(timezone.utc) - timedelta(minutes=10))
    st, body, _ = api.create(Timestamp=old)
    assert st == 400 and body["param"] == "$.headers.Timestamp"
    future = _rfc3339(datetime.now(timezone.utc) + timedelta(minutes=6))
    st, body, _ = api.create(Timestamp=future)
    assert st == 400 and body["param"] == "$.headers.Timestamp"


def test_malformed_timestamp_is_400(api):
    st, body, _ = api.create(Timestamp="yesterday")
    assert st == 400 and body["param"] == "$.headers.Timestamp"


def test_fresh_timestamp_is_accepted(api):
    st, body, _ = api.create(Timestamp=_rfc3339(datetime.now(timezone.utc) - timedelta(seconds=30)))
    assert st == 201, body


def test_request_signature_verified_when_key_configured(api, merchant):
    merchant["acp_signature_key"] = "sig-key"
    payload = {"items": [{"id": "90", "quantity": 1}]}
    raw = json.dumps(payload)
    good = base64.b64encode(hmac.new(b"sig-key", raw.encode(), hashlib.sha256).digest()).decode()
    st, body, _ = api.create(payload, Signature="bogus")
    assert st == 401 and body["param"] == "$.headers.Signature"
    # the client helper re-serialises with json.dumps -> identical bytes to `raw`
    st, body, _ = api.create(payload, Signature=good)
    assert st == 201, body


# ============================================================================ 3. idempotency
def test_idempotent_replay_same_key_same_body(api):
    payload = {"items": [{"id": "90", "quantity": 1}]}
    st1, b1, h1 = api.create(payload, key="k1")
    st2, b2, h2 = api.create(payload, key="k1")
    assert (st1, st2) == (201, 201)
    assert b1 == b2, "replay must return the stored response verbatim"
    assert "Idempotent-Replayed" not in h1
    assert h2["Idempotent-Replayed"] == "true"
    assert h2["Idempotency-Key"] == "k1"
    assert len(db_rows("SELECT id FROM sessions")) == 1, "replay must not create a second session"


def test_idempotency_key_reused_with_different_body_is_409(api):
    st1, _, _ = api.create({"items": [{"id": "90", "quantity": 1}]}, key="k2")
    st2, body, _ = api.create({"items": [{"id": "90", "quantity": 2}]}, key="k2")
    assert st1 == 201
    assert st2 == 409
    assert body["code"] == "idempotency_conflict" and body["type"] == "request_not_idempotent"


def test_failed_request_does_not_poison_idempotency_key(api, store):
    st, _, _ = api.create({"items": [{"id": "999", "quantity": 1}]}, key="k3")
    assert st == 400
    # same key, now a valid body: must be processed, not replayed as the error and not 409
    st, body, _ = api.create({"items": [{"id": "90", "quantity": 1}]}, key="k3")
    assert st == 201, body


def test_idempotency_keys_are_scoped_per_endpoint(api):
    st, body, _ = api.create(key="shared")
    sid = body["id"]
    st, body, _ = api.update(sid, {"buyer": BUYER}, key="shared")
    assert st == 200 and body.get("buyer") == BUYER


# ============================================================================ 4. validation
def test_missing_items_is_400_missing(api):
    st, body, _ = api.create({})
    assert st == 400 and body["code"] == "missing" and body["param"] == "$.items"
    st, body, _ = api.create({"items": []})
    assert st == 400 and body["code"] == "missing" and body["param"] == "$.items"


def test_zero_quantity_is_400_invalid(api):
    st, body, _ = api.create({"items": [{"id": "90", "quantity": 0}]})
    assert st == 400 and body["code"] == "invalid" and body["param"] == "$.items[0].quantity"


def test_non_numeric_id_is_400(api):
    st, body, _ = api.create({"items": [{"id": "sku-abc", "quantity": 1}]})
    assert st == 400 and body["code"] == "invalid" and body["param"] == "$.items[0].id"


def test_item_without_id_is_400_missing(api):
    st, body, _ = api.create({"items": [{"quantity": 1}]})
    assert st == 400 and body["code"] == "missing" and body["param"] == "$.items[0].id"


def test_unknown_product_is_400_pointing_at_item(api):
    st, body, _ = api.create({"items": [{"id": "999", "quantity": 1}]})
    assert st == 400
    assert body["code"] == "invalid"
    assert body["param"].startswith("$.items") and "999" in body["param"]
    assert "Invalid product" in body["message"]
    assert db_rows("SELECT id FROM sessions") == []


def test_out_of_stock_product_is_out_of_stock_code(api, store):
    store.out_of_stock_ids.add(91)
    st, body, _ = api.create({"items": [{"id": "91", "quantity": 1}]})
    assert st == 400 and body["code"] == "out_of_stock" and "91" in body["param"]


def test_quantity_above_stock_is_out_of_stock_code(api):
    st, body, _ = api.create({"items": [{"id": "90", "quantity": 99}]})
    assert st == 400 and body["code"] == "out_of_stock" and "90" in body["param"]


def test_store_unreachable_at_create_is_502(api, store):
    store.fail_cart = True
    st, body, _ = api.create()
    assert st == 502 and body["type"] == "service_unavailable"


# ============================================================================ 5. update
def test_update_with_address_and_buyer_becomes_ready(api):
    st, body, _ = api.create()
    sid = body["id"]
    st, body, hdrs = api.update(sid, {"fulfillment_address": ADDRESS, "buyer": BUYER}, key="u1")
    assert st == 200, body
    assert body["id"] == sid
    assert body["status"] == "ready_for_payment"
    assert body["fulfillment_option_id"] == "flat_rate:1"
    assert "payment_provider" not in body, "update response omits payment_provider (spec)"
    assert body["fulfillment_address"] == ADDRESS and body["buyer"] == BUYER
    tm = totals_map(body)
    assert tm["fulfillment"] == 690 and tm["subtotal"] == 3900 and tm["total"] == 4590
    assert body["messages"] == []
    assert hdrs["Idempotency-Key"] == "u1"
    # persisted: GET reflects it
    st, got, _ = api.get(sid)
    assert st == 200 and got["status"] == "ready_for_payment" and got["fulfillment_option_id"] == "flat_rate:1"


def test_update_items_requotes(api):
    st, body, _ = api.create()
    sid = body["id"]
    st, body, _ = api.update(sid, {"items": [{"id": "90", "quantity": 3}]})
    assert st == 200
    assert body["line_items"][0]["item"]["quantity"] == 3
    assert totals_map(body)["subtotal"] == 3 * 3900


def test_update_with_unknown_fulfillment_option_reports_error_message(api, ready_session):
    st, body, _ = api.update(ready_session, {"fulfillment_option_id": "flat_rate:99"})
    assert st == 200, body
    [msg] = body["messages"]
    assert msg["type"] == "error" and msg["code"] == "invalid" and msg["param"] == "$.fulfillment_option_id"
    assert msg["content_type"] == "plain" and msg["content"]
    assert body["fulfillment_option_id"] == "flat_rate:1", "falls back to the store's selected rate"


def test_update_selecting_valid_option_is_accepted(api, ready_session):
    st, body, _ = api.update(ready_session, {"fulfillment_option_id": "flat_rate:1"})
    assert st == 200 and body["messages"] == [] and body["fulfillment_option_id"] == "flat_rate:1"


def test_update_after_cancel_is_405(api):
    st, body, _ = api.create()
    sid = body["id"]
    assert api.cancel(sid)[0] == 200
    st, body, _ = api.update(sid, {"buyer": BUYER})
    assert st == 405 and body["code"] == "invalid"


def test_update_unknown_session_is_404(api):
    st, body, _ = api.update("cs_doesnotexist", {"buyer": BUYER})
    assert st == 404 and body["param"] == "$.checkout_session_id"
    assert api.get("cs_doesnotexist")[0] == 404


# ============================================================================ 6. cancel
def test_cancel_marks_session_canceled(api):
    st, body, _ = api.create()
    sid = body["id"]
    st, body, hdrs = api.cancel(sid, key="c1")
    assert st == 200 and body["status"] == "canceled" and body["id"] == sid
    assert "payment_provider" not in body
    assert hdrs["Idempotency-Key"] == "c1"
    assert api.get(sid)[1]["status"] == "canceled"


def test_cancel_twice_is_idempotent(api):
    """acp.py treats a second cancel as a no-op 200 (spec only mandates 405 for non-cancelable, i.e. completed)."""
    sid = api.create()[1]["id"]
    assert api.cancel(sid)[0] == 200
    st, body, _ = api.cancel(sid)
    assert st == 200 and body["status"] == "canceled"


def test_complete_after_cancel_is_405(api):
    sid = api.create()[1]["id"]
    api.cancel(sid)
    st, body, _ = api.complete(sid)
    assert st == 405 and body["code"] == "invalid"


def test_cancel_after_complete_is_405(api, completed_session):
    sid, _ = completed_session
    st, body, _ = api.cancel(sid)
    assert st == 405


# ============================================================================ 7. complete
def test_complete_without_address_is_not_ready_with_missing_message(api, store):
    sid = api.create()[1]["id"]
    st, body, _ = api.complete(sid)
    assert st == 200, body
    assert body["status"] == "not_ready_for_payment"
    [msg] = body["messages"]
    assert msg["type"] == "error" and msg["code"] == "missing" and msg["param"].startswith("$.fulfillment")
    assert "order" not in body
    assert store.orders == {}


def test_complete_without_payment_token_is_400(api, ready_session):
    st, body, _ = api.complete(ready_session, body={"payment_data": {"provider": "stripe"}})
    assert st == 400 and body["code"] == "missing" and body["param"] == "$.payment_data.token"


def test_complete_with_unsupported_provider_is_400(api, ready_session):
    st, body, _ = api.complete(ready_session, body={"payment_data": {"token": "x", "provider": "adyen"}})
    assert st == 400 and body["param"] == "$.payment_data.provider"


def test_complete_with_declined_token_reports_payment_declined_and_no_order(api, ready_session, store):
    st, body, _ = api.complete(ready_session, token="spt_test_declined")
    assert st == 200, body
    assert body["status"] == "ready_for_payment"
    [msg] = body["messages"]
    assert msg["type"] == "error" and msg["code"] == "payment_declined" and msg["param"] == "$.payment_data.token"
    assert "order" not in body
    assert store.orders == {}, "never create a Woo order on a declined payment"
    assert db_rows("SELECT * FROM orders") == []
    assert api.get(ready_session)[1]["status"] == "ready_for_payment"


def test_complete_success_creates_order_and_emits_webhook(api, ready_session, store):
    st, body, hdrs = api.complete(ready_session, key="cmp1")
    assert st == 200, body
    assert body["status"] == "completed"
    assert "payment_provider" not in body
    assert body["messages"] == []
    order = body["order"]
    assert order["id"] == "4242" and order["checkout_session_id"] == ready_session
    assert order["permalink_url"].startswith(f"https://gateway.test/acp/o/{MERCHANT_SLUG}.4242.")
    assert hdrs["Idempotency-Key"] == "cmp1"

    # Woo order: created pending then flipped to processing + paid with the PaymentIntent id
    woo = store.orders[4242]
    assert woo["status"] == "processing" and woo["set_paid"] is True
    assert woo["transaction_id"].startswith("pi_mock_")
    assert woo["billing"]["email"] == BUYER["email"]
    assert woo["shipping"]["address_1"] == ADDRESS["line_one"] and woo["shipping"]["postcode"] == ADDRESS["postal_code"]
    assert woo["shipping_lines"] == [{"method_id": "flat_rate", "method_title": "Flat rate", "total": "6.90"}]
    assert {md["key"]: md["value"] for md in woo["meta_data"]}["acp_checkout_session_id"] == ready_session
    assert woo["total"] == "45.90"
    assert store.cron_pokes == 1

    # our ledger + outbox
    [row] = db_rows("SELECT * FROM orders")
    assert row["woo_order_id"] == "4242" and row["status"] == "created" and row["amount"] == 4590
    assert row["currency"] == "usd" and row["email"] == BUYER["email"] and row["stripe_pi"].startswith("pi_mock_")
    [ev] = db_rows("SELECT * FROM outbox")
    assert ev["event"] == "order_created"
    payload = json.loads(ev["payload"])
    assert payload["type"] == "order_created"
    assert payload["data"] == {"type": "order", "checkout_session_id": ready_session,
                               "permalink_url": order["permalink_url"], "status": "confirmed", "refunds": []}

    # GET shows the order afterwards
    st, got, _ = api.get(ready_session)
    assert st == 200 and got["status"] == "completed" and got["order"] == order

    # same idempotency key -> replay, no second charge/order
    st, again, h2 = api.complete(ready_session, key="cmp1")
    assert st == 200 and again == body and h2["Idempotent-Replayed"] == "true"
    assert len(store.orders) == 1

    # complete again without a key on a completed session -> completed + order, still no new order
    st, again, _ = api.complete(ready_session, key=None, Idempotency_Key=None)
    assert st == 200 and again["status"] == "completed" and again["order"] == order
    assert len(store.orders) == 1


def test_complete_reuses_existing_woo_order_for_session(api, ready_session, store):
    """Order creation is idempotent on the acp_checkout_session_id meta (search before create)."""
    st, body, _ = api.complete(ready_session, key="first")
    assert body["order"]["id"] == "4242"
    # simulate a lost response: wipe our session status back to ready and complete again with a new key
    with acp._db() as c:
        c.execute("UPDATE sessions SET status='ready_for_payment', order_id=NULL WHERE id=?", (ready_session,))
    st, body, _ = api.complete(ready_session, key="second")
    assert st == 200 and body["order"]["id"] == "4242"
    assert len(store.orders) == 1


# ============================================================================ 8. quote drift
def test_price_drift_between_update_and_complete_refuses_to_charge(api, ready_session, store):
    store.prices[90] = 4900   # merchant raised the price after the agent saw the quote
    st, body, _ = api.complete(ready_session)
    assert st == 200, body
    assert body["status"] != "completed"
    [msg] = body["messages"]
    assert msg["type"] == "error" and msg["code"] == "invalid" and msg["param"] == "$.totals"
    assert totals_map(body)["total"] == 4900 + 690, "response carries the refreshed totals"
    assert "order" not in body
    assert store.orders == {} and db_rows("SELECT * FROM orders") == []
    # a bare retry is still refused: the buyer must be shown the new totals via an update first
    st, body, _ = api.complete(ready_session)
    assert st == 200 and body["status"] == "not_ready_for_payment" and "order" not in body
    st, body, _ = api.update(ready_session, {})
    assert body["status"] == "ready_for_payment" and totals_map(body)["total"] == 5590
    st, body, _ = api.complete(ready_session)
    assert st == 200 and body["status"] == "completed"
    assert db_rows("SELECT amount FROM orders")[0]["amount"] == 5590


# ============================================================================ 9. store down at order creation
def test_store_down_at_order_creation_goes_in_progress_then_retry_completes(api, ready_session, store):
    store.fail_create = True
    st, body, _ = api.complete(ready_session, key="cmp-down")
    assert st == 200, body
    assert body["status"] == "in_progress"
    assert body["order"]["id"] == ready_session and body["order"]["checkout_session_id"] == ready_session
    assert store.orders == {}

    [row] = db_rows("SELECT * FROM orders")
    assert row["status"] == "pending_store" and row["woo_order_id"] is None and row["id"] == ready_session
    pi = row["stripe_pi"]
    assert pi.startswith("pi_mock_")
    [ev] = db_rows("SELECT * FROM outbox")
    assert ev["event"] == "_retry_order" and ev["delivered"] is None
    assert json.loads(ev["payload"]) == {"session_id": ready_session, "pi": pi}
    assert api.get(ready_session)[1]["status"] == "in_progress"
    # cannot cancel while the order is being finalised
    assert api.cancel(ready_session)[0] == 405

    # store comes back; run the worker's delivery step directly on the outbox row
    store.fail_create = False
    with acp._db() as c:
        outbox_row = c.execute("SELECT * FROM outbox WHERE event='_retry_order'").fetchone()
    assert acp._deliver(outbox_row) is True

    assert 4242 in store.orders and store.orders[4242]["transaction_id"] == pi
    st, got, _ = api.get(ready_session)
    assert got["status"] == "completed" and got["order"]["id"] == "4242"
    [row] = db_rows("SELECT * FROM orders")
    assert row["id"] == "4242" and row["woo_order_id"] == "4242" and row["status"] == "created"
    events = db_rows("SELECT event, payload FROM outbox ORDER BY id")
    assert [e["event"] for e in events] == ["_retry_order", "order_updated"]
    upd = json.loads(events[1]["payload"])
    assert upd["type"] == "order_updated" and upd["data"]["status"] == "confirmed"
    assert upd["data"]["checkout_session_id"] == ready_session and ".4242." in upd["data"]["permalink_url"]


def test_deliver_posts_signed_webhook_when_url_configured(merchant, store, monkeypatch):
    merchant["openai_webhook_url"] = "https://openai-webhooks.test/hook"
    merchant["openai_webhook_key"] = "whk"
    payload = {"type": "order_created", "data": {"type": "order", "checkout_session_id": "cs_x",
                                                  "permalink_url": "https://gateway.test/acp/o/x", "status": "confirmed", "refunds": []}}
    row = {"merchant": MERCHANT_SLUG, "event": "order_created", "payload": acp._j(payload)}
    assert acp._deliver(row) is True
    [post] = store.webhook_posts
    body = post["body"]
    assert json.loads(body) == payload
    want = hmac.new(b"whk", body, hashlib.sha256).hexdigest()
    assert post["headers"]["Merchant-Signature"] == want
    assert post["headers"]["Content-Type"] == "application/json" and post["headers"]["Request-Id"]
    store.webhook_status = 500
    assert acp._deliver(row) is False


def test_deliver_without_webhook_url_marks_delivered(merchant):
    row = {"merchant": MERCHANT_SLUG, "event": "order_created", "payload": "{}"}
    assert acp._deliver(row) is True


# ============================================================================ 10. permalink page
def _permalink_path(url):
    return url.split("https://gateway.test", 1)[1]


def test_permalink_get_shows_email_gate(client, completed_session):
    _, body = completed_session
    r = client.get(_permalink_path(body["order"]["permalink_url"]))
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'name=email' in html and "<form method=post" in html
    assert "order #4242" in html and "Test Prints" in html
    assert "Status:" not in html


def test_permalink_post_wrong_email_is_rejected(client, completed_session):
    _, body = completed_session
    r = client.post(_permalink_path(body["order"]["permalink_url"]), data={"email": "someone@else.com"})
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "doesn't match" in html and "Status:" not in html


def test_permalink_post_right_email_renders_order(client, completed_session, store):
    _, body = completed_session
    r = client.post(_permalink_path(body["order"]["permalink_url"]), data={"email": "  ADA@example.com "})
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Status:" in html and "processing" in html
    assert store.catalog[90]["name"] in html and "× 1" in html
    assert "USD 45.90" in html and "USD 6.90" in html
    assert "Ada Lovelace, 1 Main St, Boston MA 02101" in html
    assert "doesn't match" not in html


def test_permalink_tampered_token_is_404(client, completed_session):
    _, body = completed_session
    path = _permalink_path(body["order"]["permalink_url"])
    slug, oid, sig = path.rsplit("/", 1)[1].split(".")
    bad_sig = ("0" if sig[0] != "0" else "1") + sig[1:]
    assert client.get(f"/acp/o/{slug}.{oid}.{bad_sig}").status_code == 404
    assert client.get(f"/acp/o/{slug}.4243.{sig}").status_code == 404, "signature is bound to the order id"
    assert client.get(f"/acp/o/other.{oid}.{sig}").status_code == 404, "signature is bound to the merchant"
    assert client.get("/acp/o/garbage").status_code == 404
    assert client.get(path).status_code == 200


# ============================================================================ 11. Woo inbound webhook
def _woo_post(client, payload, headers=None, raw=None):
    data = raw if raw is not None else json.dumps(payload)
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    return client.post(f"/acp/{MERCHANT_SLUG}/webhooks/woo", data=data, headers=h)


def test_woo_webhook_without_secret_is_accepted_and_maps_status(client, completed_session, store):
    """No webhook secret but merchant REST keys: the posted body only says *which* order changed; the status and
    refunds are re-read from the store with the merchant keys."""
    sid, _ = completed_session
    store.change_order(4242, status="completed")
    r = _woo_post(client, {"id": 4242, "status": "completed", "date_modified": "2026-09-02T12:00:00"})
    assert r.status_code == 200
    events = db_rows("SELECT event, payload FROM outbox ORDER BY id")
    assert [e["event"] for e in events] == ["order_created", "order_updated"]
    upd = json.loads(events[1]["payload"])
    assert upd == {"type": "order_updated", "data": {"type": "order", "checkout_session_id": sid,
                   "permalink_url": acp._permalink(MERCHANT_SLUG, "4242"), "status": "fulfilled", "refunds": []}}
    assert db_rows("SELECT status FROM orders")[0]["status"] == "fulfilled"
    assert ("GET", "/wp-json/wc/v3/orders/4242", None) in store.requests_log, "order re-read from the store"


@pytest.mark.parametrize("woo_status,acp_status", [("processing", "confirmed"), ("on-hold", "manual_review"),
                                                   ("cancelled", "canceled"), ("shipped", "shipped"),
                                                   ("some-plugin-status", "confirmed")])
def test_woo_webhook_status_mapping(client, completed_session, store, woo_status, acp_status):
    store.change_order(4242, status=woo_status)
    r = _woo_post(client, {"id": 4242, "status": woo_status, "date_modified": f"2026-09-02T12:00:00-{woo_status}"})
    assert r.status_code == 200
    upd = json.loads(db_rows("SELECT payload FROM outbox WHERE event='order_updated'")[0]["payload"])
    assert upd["data"]["status"] == acp_status


def test_woo_webhook_signature_enforced_when_secret_configured(client, merchant, completed_session, store):
    merchant["woo_webhook_secret"] = "woo-secret"
    store.change_order(4242, status="completed")
    payload = {"id": 4242, "status": "completed", "date_modified": "2026-09-02T12:00:00"}
    raw = json.dumps(payload)
    assert _woo_post(client, None, raw=raw).status_code == 401, "missing signature"
    assert _woo_post(client, None, raw=raw, headers={"X-WC-Webhook-Signature": "bad"}).status_code == 401
    assert db_rows("SELECT * FROM outbox WHERE event='order_updated'") == []
    good = base64.b64encode(hmac.new(b"woo-secret", raw.encode(), hashlib.sha256).digest()).decode()
    r = _woo_post(client, None, raw=raw, headers={"X-WC-Webhook-Signature": good})
    assert r.status_code == 200
    [ev] = db_rows("SELECT payload FROM outbox WHERE event='order_updated'")
    assert json.loads(ev["payload"])["data"]["status"] == "fulfilled"


def test_woo_webhook_refunds_are_mirrored_into_order_updated(client, completed_session, store):
    sid, _ = completed_session
    store.change_order(4242, refunds=[{"id": 77, "reason": "damaged", "total": "-10.00"}])
    r = _woo_post(client, {"id": 4242, "status": "processing", "date_modified": "2026-09-02T13:00:00"})
    assert r.status_code == 200
    [ev] = db_rows("SELECT payload FROM outbox WHERE event='order_updated'")
    upd = json.loads(ev["payload"])
    assert upd["data"]["refunds"] == [{"type": "original_payment", "amount": 1000}]
    assert upd["data"]["status"] == "confirmed"
    assert db_rows("SELECT refunded FROM orders")[0]["refunded"] == 1000

    # a second refund: cumulative total 1500 -> refunds[] reports the full refunded amount, ledger updated
    store.change_order(4242, refunds=[{"id": 77, "total": "-10.00"}, {"id": 78, "total": "-5.00"}])
    r = _woo_post(client, {"id": 4242, "status": "processing", "date_modified": "2026-09-02T13:30:00"})
    assert r.status_code == 200
    evs = db_rows("SELECT payload FROM outbox WHERE event='order_updated' ORDER BY id")
    assert json.loads(evs[-1]["payload"])["data"]["refunds"] == [{"type": "original_payment", "amount": 1500}]
    assert db_rows("SELECT refunded FROM orders")[0]["refunded"] == 1500

    # re-delivery after an unrelated modification -> no new refund reported, ledger unchanged
    store.change_order(4242, customer_note="gift wrap")
    r = _woo_post(client, {"id": 4242, "status": "processing", "date_modified": "2026-09-02T14:00:00"})
    evs = db_rows("SELECT payload FROM outbox WHERE event='order_updated' ORDER BY id")
    assert json.loads(evs[-1]["payload"])["data"]["refunds"] == []
    assert db_rows("SELECT refunded FROM orders")[0]["refunded"] == 1500


def test_woo_webhook_refund_on_mock_payment_does_not_call_stripe(client, completed_session, store, monkeypatch):
    called = []
    monkeypatch.setattr(acp, "stripe_refund", lambda *a, **k: called.append(a))
    store.change_order(4242, status="refunded", refunds=[{"id": 1, "total": "-45.90"}])
    _woo_post(client, {"id": 4242, "status": "refunded", "date_modified": "2026-09-02T15:00:00"})
    assert called == [], "pi_mock_ intents must not hit Stripe"
    upd = json.loads(db_rows("SELECT payload FROM outbox WHERE event='order_updated'")[0]["payload"])
    assert upd["data"]["status"] == "canceled" and upd["data"]["refunds"] == [{"type": "original_payment", "amount": 4590}]


def test_woo_webhook_refund_on_real_payment_mirrors_to_stripe(client, completed_session, store, monkeypatch):
    with acp._db() as c:
        c.execute("UPDATE orders SET stripe_pi='pi_real_123'")
    called = []
    monkeypatch.setattr(acp, "stripe_refund", lambda m, pi, amount=None: called.append((pi, amount)) or {"id": "re_1"})
    store.change_order(4242, refunds=[{"id": 1, "total": "-10.00"}])
    _woo_post(client, {"id": 4242, "status": "processing", "date_modified": "2026-09-02T15:00:00"})
    assert called == [("pi_real_123", 1000)]


def test_woo_webhook_forged_refund_never_reaches_stripe(client, completed_session, store, monkeypatch):
    """SECURITY: /webhooks/woo is unauthenticated when no woo_webhook_secret is set (Lineal's config). A forged
    body claiming a refund must not trigger a Stripe refund — the store (which has no refund) is the truth."""
    with acp._db() as c:
        c.execute("UPDATE orders SET stripe_pi='pi_real_123'")
    called = []
    monkeypatch.setattr(acp, "stripe_refund", lambda m, pi, amount=None: called.append((pi, amount)) or {"id": "re_1"})
    r = _woo_post(client, {"id": 4242, "status": "refunded", "date_modified": "2026-09-02T16:00:00",
                           "refunds": [{"id": 1, "total": "-45.90"}]})
    assert r.status_code == 200
    assert called == [], "forged refund must not be mirrored to Stripe"
    assert db_rows("SELECT status, refunded FROM orders")[0] == {"status": "confirmed", "refunded": 0}
    evs = db_rows("SELECT payload FROM outbox WHERE event='order_updated'")
    assert all(json.loads(e["payload"])["data"]["refunds"] == [] for e in evs)


def test_woo_webhook_unsigned_without_keys_is_ignored(client, merchant, completed_session, store):
    """No REST keys and no webhook secret: nothing in the body can be verified, so it is dropped."""
    merchant.pop("woo_ck"); merchant.pop("woo_cs")
    store.change_order(4242, status="completed")
    r = _woo_post(client, {"id": 4242, "status": "completed", "date_modified": "2026-09-02T12:00:00"})
    assert r.status_code == 200
    assert db_rows("SELECT * FROM outbox WHERE event='order_updated'") == []
    assert db_rows("SELECT status FROM orders")[0]["status"] == "created"


def test_woo_webhook_duplicate_delivery_is_ignored(client, completed_session, store):
    store.change_order(4242, status="completed")
    payload = {"id": 4242, "status": "completed", "date_modified": "2026-09-02T12:00:00"}
    assert _woo_post(client, payload).status_code == 200
    assert _woo_post(client, payload).status_code == 200
    assert len(db_rows("SELECT * FROM outbox WHERE event='order_updated'")) == 1
    # a genuinely new modification of the same order is processed
    store.change_order(4242, status="completed", customer_note="x")
    assert _woo_post(client, dict(payload, date_modified="2026-09-02T12:00:01")).status_code == 200
    assert len(db_rows("SELECT * FROM outbox WHERE event='order_updated'")) == 2


def test_woo_webhook_ping_without_id_is_200(client):
    assert _woo_post(client, {"webhook_id": 5}).status_code == 200
    assert _woo_post(client, None, raw="not json").status_code == 200
    assert db_rows("SELECT * FROM outbox") == []


def test_woo_webhook_for_foreign_order_is_ignored(client, completed_session):
    r = _woo_post(client, {"id": 9999, "status": "completed", "date_modified": "2026-09-02T12:00:00"})
    assert r.status_code == 200
    assert db_rows("SELECT * FROM outbox WHERE event='order_updated'") == []


def test_woo_webhook_unknown_merchant_is_404(client):
    r = client.post("/acp/nope/webhooks/woo", data=json.dumps({"id": 1}), headers={"Content-Type": "application/json"})
    assert r.status_code == 404


# ============================================================================ 12. health
def test_health_endpoint_shape(client, store):
    r = client.get(f"/acp/{MERCHANT_SLUG}/health")
    assert r.status_code == 200
    j = r.get_json()
    assert j == {"ok": True, "merchant": MERCHANT_SLUG, "api_version": acp.API_VERSION, "mock_payments": True,
                 "store_api": "ok", "woo_keys": True, "stripe_key": False, "openai_webhook": False}


def test_health_reports_store_failure_as_503(client, store):
    store.fail_cart = True
    r = client.get(f"/acp/{MERCHANT_SLUG}/health")
    assert r.status_code == 503
    j = r.get_json()
    assert j["ok"] is False and j["store_api"].startswith("fail:")


def test_health_unknown_merchant_is_404(client):
    r = client.get("/acp/nope/health")
    assert r.status_code == 404 and r.get_json()["ok"] is False


# ============================================================================ rate limit / Stripe inbound / status / reconcile
def test_rate_limit_returns_429_error_body(api, monkeypatch):
    monkeypatch.setattr(acp, "RATE_LIMIT", 2)
    acp._buckets.clear()
    assert api.create()[0] == 201
    assert api.create()[0] == 201
    st, body, _ = api.create()
    assert st == 429 and body["code"] == "rate_limited" and body["type"] and body["message"]


def _stripe_post(client, event, secret=None, ts=None):
    raw = json.dumps(event)
    h = {"Content-Type": "application/json"}
    if secret:
        ts = ts or int(acp._now())
        v1 = hmac.new(secret.encode(), f"{ts}.{raw}".encode(), hashlib.sha256).hexdigest()
        h["Stripe-Signature"] = f"t={ts},v1={v1}"
    return client.post(f"/acp/{MERCHANT_SLUG}/webhooks/stripe", data=raw, headers=h)


def test_stripe_webhook_charge_refunded_records_woo_refund_and_emits_order_updated(client, merchant, completed_session, store):
    sid, _ = completed_session
    merchant["stripe_webhook_secret"] = "whsec_test"
    pi = db_rows("SELECT stripe_pi FROM orders")[0]["stripe_pi"]
    ev = {"id": "evt_1", "type": "charge.refunded", "data": {"object": {"id": "ch_1", "payment_intent": pi, "amount_refunded": 1000}}}
    assert _stripe_post(client, ev).status_code == 401, "missing signature"
    assert _stripe_post(client, ev, secret="wrong").status_code == 401
    assert _stripe_post(client, ev, secret="whsec_test", ts=int(acp._now()) - 3600).status_code == 401, "stale timestamp"
    assert _stripe_post(client, ev, secret="whsec_test").status_code == 200
    # partial refund: Woo refund record created (api_refund false), ledger + webhook updated, status unchanged
    assert store.orders[4242]["refunds"] == [{"id": 700, "amount": "10.00", "reason": "Refunded via Stripe", "api_refund": False}]
    row = db_rows("SELECT * FROM orders")[0]
    assert row["refunded"] == 1000 and row["status"] == "created"
    upd = json.loads(db_rows("SELECT payload FROM outbox WHERE event='order_updated'")[-1]["payload"])
    assert upd["data"] == {"type": "order", "checkout_session_id": sid, "permalink_url": acp._permalink(MERCHANT_SLUG, "4242"),
                           "status": "created", "refunds": [{"type": "original_payment", "amount": 1000}]}
    # duplicate event id ignored
    assert _stripe_post(client, ev, secret="whsec_test").status_code == 200
    assert len(store.orders[4242]["refunds"]) == 1
    # full refund -> canceled
    ev2 = {"id": "evt_2", "type": "charge.refunded", "data": {"object": {"id": "ch_1", "payment_intent": pi, "amount_refunded": 4590}}}
    assert _stripe_post(client, ev2, secret="whsec_test").status_code == 200
    row = db_rows("SELECT * FROM orders")[0]
    assert row["refunded"] == 4590 and row["status"] == "canceled"
    assert store.orders[4242]["refunds"][-1]["amount"] == "35.90"
    upd = json.loads(db_rows("SELECT payload FROM outbox WHERE event='order_updated'")[-1]["payload"])
    assert upd["data"]["status"] == "canceled" and upd["data"]["refunds"] == [{"type": "original_payment", "amount": 4590}]


def test_stripe_webhook_dispute_puts_order_on_hold(client, merchant, completed_session, store):
    merchant["stripe_webhook_secret"] = "whsec_test"
    pi = db_rows("SELECT stripe_pi FROM orders")[0]["stripe_pi"]
    ev = {"id": "evt_d1", "type": "charge.dispute.created", "data": {"object": {"id": "dp_1", "payment_intent": pi}}}
    assert _stripe_post(client, ev, secret="whsec_test").status_code == 200
    assert store.orders[4242]["status"] == "on-hold"
    assert db_rows("SELECT status FROM orders")[0]["status"] == "manual_review"
    upd = json.loads(db_rows("SELECT payload FROM outbox WHERE event='order_updated'")[-1]["payload"])
    assert upd["data"]["status"] == "manual_review"


def test_stripe_webhook_unsigned_is_verified_against_stripe_or_dropped(client, merchant, completed_session, store):
    """SECURITY: without stripe_webhook_secret an unsigned POST could put a store order on hold. The event must be
    re-read from Stripe by id (merchant key) — and dropped entirely when there is no key or Stripe doesn't know it."""
    pi = db_rows("SELECT stripe_pi FROM orders")[0]["stripe_pi"]
    forged = {"id": "evt_forged", "type": "charge.dispute.created", "data": {"object": {"id": "dp_1", "payment_intent": pi}}}
    # no stripe key on the merchant: ignored, no Stripe call, order untouched
    assert _stripe_post(client, forged).status_code == 200
    assert store.orders[4242]["status"] == "processing" and store.stripe_calls == []
    assert db_rows("SELECT status FROM orders")[0]["status"] == "created"
    # key present, Stripe has no such event: ignored
    merchant["stripe_secret_key"] = "rk_test_x"
    assert _stripe_post(client, forged).status_code == 200
    assert store.stripe_calls == [("GET", "/v1/events/evt_forged", ("rk_test_x", ""))]
    assert store.orders[4242]["status"] == "processing"
    # a real event id: processed from Stripe's copy, not from the posted body
    store.stripe_events["evt_real"] = {"id": "evt_real", "type": "charge.dispute.created", "data": {"object": {"id": "dp_2", "payment_intent": pi}}}
    assert _stripe_post(client, {"id": "evt_real", "type": "charge.refunded", "data": {"object": {"payment_intent": pi, "amount_refunded": 4590}}}).status_code == 200
    assert store.orders[4242]["status"] == "on-hold", "acted on Stripe's version (dispute), not the posted one (refund)"
    assert db_rows("SELECT status, refunded FROM orders")[0] == {"status": "manual_review", "refunded": 0}


def test_stripe_webhook_ignores_unknown_intent_and_ping(client, completed_session):
    assert _stripe_post(client, {"id": "evt_x", "type": "charge.refunded", "data": {"object": {"payment_intent": "pi_other"}}}).status_code == 200
    assert _stripe_post(client, {"object": "event"}).status_code == 200
    assert db_rows("SELECT * FROM outbox WHERE event='order_updated'") == []


def test_status_endpoint_requires_bearer_and_summarises(client, api, completed_session):
    assert client.get(f"/acp/{MERCHANT_SLUG}/status").status_code == 401
    r = client.get(f"/acp/{MERCHANT_SLUG}/status", headers=api.headers())
    assert r.status_code == 200
    j = r.get_json()
    assert j["merchant"] == MERCHANT_SLUG and j["sessions"] == 1 and j["sessions_by_status"] == {"completed": 1}
    assert j["orders"][0]["woo_order_id"] == "4242" and j["net_revenue_minor"] == 4590 and j["webhooks_pending"] == 1
    assert j["endpoints"]["base"] == f"https://gateway.test/acp/{MERCHANT_SLUG}"


def test_reconcile_requires_admin_key_and_flags_pending_store(client, api, ready_session, store, monkeypatch):
    monkeypatch.setenv("ACP_ADMIN_KEY", "adm")
    store.fail_create = True
    assert api.complete(ready_session)[1]["status"] == "in_progress"
    assert client.get(f"/acp/{MERCHANT_SLUG}/reconcile").status_code == 401
    assert client.get(f"/acp/{MERCHANT_SLUG}/reconcile?key=wrong").status_code == 401
    r = client.get(f"/acp/{MERCHANT_SLUG}/reconcile", headers={"X-Admin-Key": "adm"})
    assert r.status_code == 200
    j = r.get_json()
    assert j["checked"] == 1 and j["ok"] == 0
    assert j["issues"][0]["issue"].startswith("no woo order") and j["issues"][0]["session"] == ready_session
    # ?fix=1 only refunds orphans older than 2h — this one is fresh, so nothing is fixed
    j = client.get(f"/acp/{MERCHANT_SLUG}/reconcile?fix=1", headers={"X-Admin-Key": "adm"}).get_json()
    assert j["fixed"] == []
    with acp._db() as c:
        c.execute("UPDATE orders SET created = created - 8000")
    j = client.get(f"/acp/{MERCHANT_SLUG}/reconcile?fix=1", headers={"X-Admin-Key": "adm"}).get_json()
    assert j["fixed"][0]["order"] == ready_session and j["fixed"][0]["refund"] == "re_mock"
    assert db_rows("SELECT status, refunded FROM orders")[0] == {"status": "canceled", "refunded": 4590}


def test_reconcile_reports_clean_completed_order(client, completed_session, monkeypatch):
    monkeypatch.setenv("ACP_ADMIN_KEY", "adm")
    j = client.get(f"/acp/{MERCHANT_SLUG}/reconcile", headers={"X-Admin-Key": "adm"}).get_json()
    assert j["checked"] == 1 and j["ok"] == 1 and j["issues"] == [] and j["outbox_failing"] == []


# ============================================================================ robustness (found while writing the suite)
def test_store_unreachable_during_complete_requote_is_502_not_500(api, ready_session, store):
    """A transport failure inside _requote before payment must be a 502 service_unavailable AND release the
    idempotency key, so the agent's retry with the same key can succeed once the store is back."""
    store.fail_cart = True
    st, body, _ = api.complete(ready_session, key="cmp-fail")
    assert st == 502, (st, body)
    assert body["type"] == "service_unavailable"
    store.fail_cart = False
    st, body, _ = api.complete(ready_session, key="cmp-fail")
    assert st == 200 and body["status"] == "completed", "key must be released after a failed attempt"


# ============================================================================ regressions for review findings (2 Sep)
def test_update_on_in_progress_session_is_405(api, ready_session, store):
    store.fail_create = True
    assert api.complete(ready_session, key="ip-1")[1]["status"] == "in_progress"
    st, body, _ = api.update(ready_session, {"items": [{"id": "90", "quantity": 3}]})
    assert st == 405, "a paid-but-unconfirmed order must not be re-quoted"


def test_complete_again_on_in_progress_does_not_charge_twice(api, ready_session, store):
    store.fail_create = True
    api.complete(ready_session, key="ip-2")
    [row] = db_rows("SELECT * FROM orders")
    st, body, _ = api.complete(ready_session, key="ip-3")   # new idempotency key, agent retrying
    assert st == 200 and body["status"] == "in_progress"
    assert body["order"]["checkout_session_id"] == ready_session
    assert [m["code"] for m in body["messages"]] == ["in_progress"]
    assert db_rows("SELECT * FROM orders") == [row], "no second payment/order row"


def test_price_drift_forces_not_ready_until_next_update(api, ready_session, store):
    store.prices[90] = 4900
    st, body, _ = api.complete(ready_session, key="drift-1")
    assert st == 200 and body["status"] == "not_ready_for_payment"
    assert body["messages"][0]["param"] == "$.totals"
    # a bare retry with a fresh key still refuses: readiness only returns via an update the agent (and buyer) sees
    st, body, _ = api.complete(ready_session, key="drift-2")
    assert body["status"] == "not_ready_for_payment" and not body.get("order")
    st, body, _ = api.update(ready_session, {})
    assert body["status"] == "ready_for_payment"
    assert next(t["amount"] for t in body["totals"] if t["type"] == "total") == 4900 + 690
    assert api.complete(ready_session, key="drift-3")[1]["status"] == "completed"


def test_retry_worker_skips_orders_refunded_by_reconcile(api, ready_session, store):
    store.fail_create = True
    api.complete(ready_session, key="rc-1")
    with acp._db_lock, acp._db() as c:            # what reconcile ?fix=1 does
        c.execute("UPDATE orders SET status='canceled', refunded=amount")
    store.fail_create = False
    with acp._db() as c:
        row = c.execute("SELECT * FROM outbox WHERE event='_retry_order'").fetchone()
    assert acp._deliver(row) is True
    assert store.orders == {}, "store order must never be created after the buyer was refunded"


def test_give_up_refunds_and_cancels(api, ready_session, store):
    store.fail_create = True
    api.complete(ready_session, key="gu-1")
    with acp._db() as c:
        row = c.execute("SELECT * FROM outbox WHERE event='_retry_order'").fetchone()
    acp._give_up_order(row)
    [o] = db_rows("SELECT * FROM orders")
    assert o["status"] == "canceled" and o["refunded"] == o["amount"]
    assert api.get(ready_session)[1]["status"] == "canceled"
    evs = [json.loads(r["payload"]) for r in db_rows("SELECT * FROM outbox WHERE event='order_updated'")]
    assert evs and evs[-1]["data"]["status"] == "canceled" and evs[-1]["data"]["refunds"][0]["amount"] == o["amount"]


def test_variation_line_items_do_not_send_product_id(api, ready_session, store):
    api.complete(ready_session, key="var-1")
    [(oid, order)] = store.orders.items()
    li = order["line_items"][0]
    ids = {k: v for k, v in li.items() if k in ("product_id", "variation_id")}
    assert list(ids.values()) == [90], "exactly one of product_id / variation_id, never product_id=variation_id"


def test_boolean_quantity_rejected(api):
    st, body, _ = api.create({"items": [{"id": "90", "quantity": True}]})
    assert st == 400 and body["param"] == "$.items[0].quantity"


def test_onboarding_cannot_hijack_configured_merchant(onboarding_env):
    slug_cfg, checks = acp.onboard_merchant(f"{MERCHANT_SLUG}.evil.example")
    slug, cfg = slug_cfg
    assert cfg is None and checks["slug"]["ok"] is False
