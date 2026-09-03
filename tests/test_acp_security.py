"""
Regression tests for the security / correctness review of scanner/acp.py (3 Sep).
Each test names the failure it guards against. No network (see conftest.py).
"""
import ipaddress
import json

import pytest

import acp
from tests.conftest import ADDRESS, BUYER, BASE_MERCHANT, MERCHANT_SLUG, Api, db_rows


def total_of(body):
    return next(t["amount"] for t in body["totals"] if t["type"] == "total")


# ============================================================================ request pipeline
def test_rate_limit_keys_on_proxy_seen_ip_not_client_supplied_xff(api, monkeypatch):
    """X-Forwarded-For's leftmost entry is whatever the client sends; Render appends the real address last.
    Rotating a fake leftmost entry must not give a fresh rate-limit bucket."""
    monkeypatch.setattr(acp, "RATE_LIMIT", 2)
    acp._buckets.clear()
    for i in range(2):
        assert api.create(X_Forwarded_For=f"10.0.0.{i}, 203.0.113.9")[0] == 201
    st, body, _ = api.create(X_Forwarded_For="10.0.0.99, 203.0.113.9")
    assert st == 429 and body["code"] == "rate_limited"


def test_ip_allowlist_cannot_be_spoofed_via_xff(api, monkeypatch):
    monkeypatch.setattr(acp, "ENFORCE_IP", True)
    monkeypatch.setattr(acp, "_openai_nets", lambda block=False: [ipaddress.ip_network("23.102.140.112/28")])
    # attacker puts an OpenAI address first; the proxy appended the real (non-OpenAI) address last
    st, body, _ = api.create(X_Forwarded_For="23.102.140.115, 198.51.100.7")
    assert st == 403 and body["code"] == "forbidden"
    # genuine OpenAI egress as seen by the proxy
    assert api.create(X_Forwarded_For="1.2.3.4, 23.102.140.115")[0] == 201
    assert api.create(X_Forwarded_For="23.102.140.115")[0] == 201


def test_configured_signature_key_requires_the_header(api, merchant):
    """Fail closed: with acp_signature_key set, simply omitting the Signature header used to skip verification."""
    merchant["acp_signature_key"] = "sig-key"
    st, body, _ = api.create()
    assert st == 401 and body["code"] == "missing" and body["param"] == "$.headers.Signature"


def test_oversized_body_is_413(api, client):
    big = json.dumps({"items": [{"id": "90", "quantity": 1}], "pad": "x" * (acp.MAX_BODY + 10)})
    r = client.post(f"/acp/{MERCHANT_SLUG}/checkout_sessions", data=big, headers=api.headers())
    assert r.status_code == 413 and r.get_json()["code"] == "invalid"
    r = client.post(f"/acp/{MERCHANT_SLUG}/webhooks/woo", data=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    r = client.post(f"/acp/{MERCHANT_SLUG}/webhooks/stripe", data=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    r = client.post("/acp/onboard", data=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_item_count_quantity_and_idempotency_key_are_bounded(api, store):
    st, body, _ = api.create({"items": [{"id": "90", "quantity": 1}] * (acp.MAX_ITEMS + 1)})
    assert st == 400 and body["param"] == "$.items"
    assert store.requests_log == [], "rejected before any store call"
    st, body, _ = api.create({"items": [{"id": "90", "quantity": acp.MAX_QTY + 1}]})
    assert st == 400 and body["param"] == "$.items[0].quantity"
    st, body, _ = api.create(key="k" * 300)
    assert st == 400 and body["param"] == "$.headers.Idempotency-Key"


def test_buyer_and_address_must_be_flat_objects(api):
    st, body, _ = api.create({"items": [{"id": "90", "quantity": 1}], "buyer": "ada"})
    assert st == 400 and body["code"] == "invalid" and body["param"] == "$.buyer"
    st, body, _ = api.create({"items": [{"id": "90", "quantity": 1}], "fulfillment_address": ["x"]})
    assert st == 400 and body["param"] == "$.fulfillment_address"
    st, body, _ = api.create({"items": [{"id": "90", "quantity": 1}], "buyer": {"email": "a" * (acp.MAX_STR + 1)}})
    assert st == 400 and body["param"] == "$.buyer.email"
    st, body, _ = api.create({"items": [{"id": "90", "quantity": 1}], "buyer": {"nested": {"deep": 1}}})
    assert st == 400 and body["param"] == "$.buyer.nested"
    sid = api.create()[1]["id"]
    st, body, _ = api.update(sid, {"buyer": [1, 2]})
    assert st == 400 and body["param"] == "$.buyer"
    st, body, _ = api.update(sid, {"fulfillment_option_id": {"x": 1}})
    assert st == 400 and body["param"] == "$.fulfillment_option_id"
    st, body, _ = api.update(sid, {"fulfillment_address": ADDRESS, "buyer": BUYER})
    assert st == 200 and body["status"] == "ready_for_payment"
    st, body, _ = api.complete(sid, body={"payment_data": "spt_x"})
    assert st == 400 and body["param"] == "$.payment_data"


# ============================================================================ payment
def test_stripe_idempotency_key_is_per_token_so_a_decline_does_not_brick_the_session(merchant, store, monkeypatch):
    """Stripe replays the first outcome for an idempotency key (declines included) and rejects the same key with
    different params. A key of just the session id meant: one declined card -> the session can never be paid."""
    monkeypatch.setattr(acp, "MOCK_PAY", False)
    merchant["stripe_secret_key"] = "rk_test_x"
    a = acp._stripe_charge(merchant, "spt_declined_card", 4590, "usd", "cs_1", "ada@example.com")
    b = acp._stripe_charge(merchant, "spt_second_card", 4590, "usd", "cs_1", "ada@example.com")
    c = acp._stripe_charge(merchant, "spt_second_card", 4590, "usd", "cs_1", "ada@example.com")
    assert a["ok"] and b["ok"] and c["ok"]
    keys = [p["headers"]["Idempotency-Key"] for p in store.stripe_pis]
    assert keys[0] != keys[1], "a new token must get a new PaymentIntent"
    assert keys[1] == keys[2], "the same token+amount replays (no double charge on a lost response)"
    assert all(k.startswith("acp-") and len(k) < 60 for k in keys)
    d = store.stripe_pis[0]["data"]
    assert d["amount"] == 4590 and d["currency"] == "usd" and d["payment_method_data[shared_payment_granted_token]"] == "spt_declined_card"


def test_concurrent_completes_charge_once(api, ready_session, store, monkeypatch):
    """Two /complete calls with different idempotency keys (a retry racing the original): the second must find the
    charge lock and answer in_progress, never reach Stripe, never create a second order."""
    charges, nested = [], {}
    real = acp._stripe_charge

    def racing_charge(m, spt, amount, currency, session_id, email=None):
        charges.append(spt)
        if len(charges) == 1:   # while the first charge is "in flight", a second complete arrives
            nested["resp"] = api.complete(session_id, token="spt_second", key="race-2")
        return real(m, spt, amount, currency, session_id, email)

    monkeypatch.setattr(acp, "_stripe_charge", racing_charge)
    st, body, _ = api.complete(ready_session, token="spt_first", key="race-1")
    assert st == 200 and body["status"] == "completed"
    assert charges == ["spt_first"], "exactly one charge"
    nst, nbody, _ = nested["resp"]
    assert nst == 200 and nbody["status"] == "in_progress" and "order" in nbody
    assert [m["code"] for m in nbody["messages"]] == ["in_progress"]
    assert len(store.orders) == 1 and len(db_rows("SELECT * FROM orders")) == 1


def test_declined_payment_releases_the_charge_lock(api, ready_session, store):
    st, body, _ = api.complete(ready_session, token="spt_test_declined", key="d1")
    assert body["status"] == "ready_for_payment"
    assert db_rows("SELECT status FROM sessions")[0]["status"] == "ready_for_payment"
    st, body, _ = api.complete(ready_session, token="spt_test_ok", key="d2")
    assert st == 200 and body["status"] == "completed" and len(store.orders) == 1


def test_stale_charge_lock_without_ledger_row_is_recovered(api, ready_session, store):
    """A crash between the charge lock and the ledger write leaves in_progress with no orders row. A fresh lock is
    respected (concurrent call), an old one is released so the buyer is not stuck forever."""
    with acp._db() as c:
        c.execute("UPDATE sessions SET status='in_progress', updated=? WHERE id=?", (acp._now(), ready_session))
    st, body, _ = api.complete(ready_session, key="s1")
    assert body["status"] == "in_progress" and store.orders == {}
    with acp._db() as c:
        c.execute("UPDATE sessions SET updated=? WHERE id=?", (acp._now() - acp.CHARGE_LOCK_STALE - 1, ready_session))
    st, body, _ = api.complete(ready_session, key="s2")
    assert st == 200 and body["status"] == "completed" and len(store.orders) == 1


# ============================================================================ money math
def test_store_decimals_are_normalised_to_iso_minor_units(api, store):
    """Woo reports amounts in the store's configured decimals. A USD store set to 0 decimals reports '39' for $39;
    charging Stripe 39 cents would be a 100x under-charge. JPY (0 ISO decimals) must pass through unchanged."""
    store.CURRENCY = dict(store.CURRENCY, currency_minor_unit=0)
    store.catalog[90]["price"] = 39
    store.SHIP_RATE = dict(store.SHIP_RATE, price="7")
    st, body, _ = api.create({"items": [{"id": "90", "quantity": 2}], "fulfillment_address": ADDRESS, "buyer": BUYER})
    assert st == 201, body
    assert body["line_items"][0]["base_amount"] == 7800 and body["line_items"][0]["total"] == 7800
    assert body["fulfillment_options"][0]["total"] == 700
    assert total_of(body) == 8500
    st, body, _ = api.complete(body["id"])
    assert body["status"] == "completed"
    assert db_rows("SELECT amount FROM orders")[0]["amount"] == 8500
    assert store.orders[4242]["shipping_lines"][0]["total"] == "7.00"

    store.CURRENCY = dict(store.CURRENCY, currency_code="JPY", currency_minor_unit=0)
    store.catalog[91]["price"] = 3900
    store.SHIP_RATE = dict(store.SHIP_RATE, price="690")
    st, body, _ = api.create({"items": [{"id": "91", "quantity": 1}], "fulfillment_address": ADDRESS, "buyer": BUYER})
    assert body["currency"] == "jpy" and total_of(body) == 4590
    st, body, _ = api.complete(body["id"])
    assert body["status"] == "completed"
    assert store.orders[4243]["shipping_lines"][0]["total"] == "690", "JPY has no decimals"
    assert db_rows("SELECT amount FROM orders WHERE currency='jpy'")[0]["amount"] == 4590


def test_refund_amounts_use_the_order_currency_decimals(client, completed_session, store, monkeypatch):
    with acp._db() as c:
        c.execute("UPDATE orders SET stripe_pi='pi_real_123', currency='jpy', amount=4590")
    called = []
    monkeypatch.setattr(acp, "stripe_refund", lambda m, pi, amount=None: called.append((pi, amount)) or {"id": "re_1"})
    store.change_order(4242, refunds=[{"id": 1, "total": "-690"}])
    client.post(f"/acp/{MERCHANT_SLUG}/webhooks/woo", data=json.dumps({"id": 4242, "date_modified": "x"}),
                headers={"Content-Type": "application/json"})
    assert called == [("pi_real_123", 690)]


def test_woo_refund_larger_than_the_charge_is_capped(client, completed_session, store, monkeypatch):
    with acp._db() as c:
        c.execute("UPDATE orders SET stripe_pi='pi_real_123'")
    called = []
    monkeypatch.setattr(acp, "stripe_refund", lambda m, pi, amount=None: called.append((pi, amount)) or {"id": "re_1"})
    store.change_order(4242, refunds=[{"id": 1, "total": "-99.00"}])
    client.post(f"/acp/{MERCHANT_SLUG}/webhooks/woo", data=json.dumps({"id": 4242, "date_modified": "x"}),
                headers={"Content-Type": "application/json"})
    assert called == [("pi_real_123", 4590)], "never ask Stripe for more than was charged"


def test_zero_total_is_never_charged(api, store):
    store.catalog[90]["price"] = 0
    store.SHIP_RATE = dict(store.SHIP_RATE, price="0")
    st, body, _ = api.create({"items": [{"id": "90", "quantity": 1}], "fulfillment_address": ADDRESS, "buyer": BUYER})
    assert total_of(body) == 0
    st, body, _ = api.complete(body["id"])
    assert st == 400 and body["param"] == "$.totals" and store.orders == {}


# ============================================================================ multi-tenancy
@pytest.fixture
def two_merchants(monkeypatch, store, merchant):
    other = json.loads(json.dumps(BASE_MERCHANT)); other["bearer_key"] = "other-bearer"
    monkeypatch.setenv("ACP_MERCHANTS", json.dumps({MERCHANT_SLUG: acp.merchants()[MERCHANT_SLUG], "u": other}))
    monkeypatch.setattr(acp, "_MERCHANTS", None)
    acp.merchants()
    return "u"


def test_same_store_order_number_at_two_merchants_keeps_both_ledger_rows(client, api, two_merchants, store):
    """orders.id is the store's order number; every Woo store has an order #4242. The ledger key must include the
    merchant or the second merchant's order silently replaces the first's (refunds/webhooks then go missing)."""
    other = Api(client); other.base = f"/acp/{two_merchants}"
    other_headers = lambda **kw: dict(api.headers(**kw), Authorization="Bearer other-bearer")  # noqa: E731
    # merchant t: order 4242
    sid_t = api.create({"items": [{"id": "90", "quantity": 1}], "fulfillment_address": ADDRESS, "buyer": BUYER})[1]["id"]
    assert api.complete(sid_t)[1]["order"]["id"] == "4242"
    # merchant u: its store also numbers this order 4242
    store.next_order_id = 4242
    r = client.post(f"{other.base}/checkout_sessions", data=json.dumps({"items": [{"id": "91", "quantity": 1}],
                    "fulfillment_address": ADDRESS, "buyer": BUYER}), headers=other_headers())
    sid_u = r.get_json()["id"]
    r = client.post(f"{other.base}/checkout_sessions/{sid_u}/complete",
                    data=json.dumps({"payment_data": {"token": "spt_ok", "provider": "stripe"}}), headers=other_headers())
    assert r.status_code == 200 and r.get_json()["order"]["id"] == "4242"
    rows = db_rows("SELECT merchant, id, session_id, amount FROM orders ORDER BY merchant")
    assert [(r["merchant"], r["id"], r["session_id"]) for r in rows] == [(MERCHANT_SLUG, "4242", sid_t), (two_merchants, "4242", sid_u)]
    # a refund at merchant u must not touch merchant t's ledger row
    store.change_order(4242, refunds=[{"id": 1, "total": "-10.00"}])
    client.post(f"/acp/{two_merchants}/webhooks/woo", data=json.dumps({"id": 4242, "date_modified": "y"}),
                headers={"Content-Type": "application/json"})
    by = {r["merchant"]: r["refunded"] for r in db_rows("SELECT merchant, refunded FROM orders")}
    assert by == {MERCHANT_SLUG: 0, two_merchants: 1000}


def test_orders_table_is_migrated_to_composite_key(tmp_path, monkeypatch):
    import sqlite3
    p = str(tmp_path / "old.sqlite")
    with sqlite3.connect(p) as c:
        c.execute("CREATE TABLE orders (id TEXT PRIMARY KEY, merchant TEXT, session_id TEXT, woo_order_id TEXT, stripe_pi TEXT, "
                  "amount INTEGER, currency TEXT, status TEXT, email TEXT, refunded INTEGER DEFAULT 0, created REAL)")
        c.execute("INSERT INTO orders VALUES ('4242','a','cs_a','4242','pi_a',100,'usd','created','',0,1.0)")
    monkeypatch.setattr(acp, "DB_PATH", p)
    acp.init_db()
    with sqlite3.connect(p) as c:
        c.execute("INSERT INTO orders VALUES ('4242','b','cs_b','4242','pi_b',200,'usd','created','',0,2.0)")
        assert c.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 2
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO orders VALUES ('4242','a','cs_x','4242','pi_x',1,'usd','created','',0,3.0)")


# ============================================================================ reconcile
def test_reconcile_fix_never_refunds_twice(client, api, ready_session, store, monkeypatch):
    monkeypatch.setenv("ACP_ADMIN_KEY", "adm")
    store.fail_create = True
    assert api.complete(ready_session)[1]["status"] == "in_progress"
    with acp._db() as c:
        c.execute("UPDATE orders SET created = created - 8000")
    refunds = []
    monkeypatch.setattr(acp, "stripe_refund", lambda m, pi, amount=None: refunds.append(pi) or {"id": "re_1"})
    with acp._db() as c:
        c.execute("UPDATE orders SET stripe_pi='pi_real_1'")
    monkeypatch.setattr(acp, "_stripe_pi_status", lambda m, pi: "succeeded")
    j = client.get(f"/acp/{MERCHANT_SLUG}/reconcile?fix=1", headers={"X-Admin-Key": "adm"}).get_json()
    assert [f["order"] for f in j["fixed"]] == [ready_session] and refunds == ["pi_real_1"]
    j = client.get(f"/acp/{MERCHANT_SLUG}/reconcile?fix=1", headers={"X-Admin-Key": "adm"}).get_json()
    assert j["fixed"] == [] and refunds == ["pi_real_1"], "an already-canceled (refunded) row is not refunded again"
    assert j["ok"] == 1 and j["issues"] == []


# ============================================================================ onboarding
@pytest.mark.parametrize("url", ["http://169.254.169.254/latest/meta-data/", "10.0.0.5", "localhost",
                                 "https://127.0.0.1:8080/", "[::1]", "http://user:pw@192.168.1.1/",
                                 "store_test", "x" * 300 + ".com"])
def test_onboarding_refuses_private_or_malformed_hosts(onboarding_env, store, monkeypatch, url):
    """SSRF: onboarding fetches ~8 URLs on the host a stranger types. Loopback, link-local (cloud metadata),
    private ranges and IP literals of those kinds must be refused before any request is made."""
    monkeypatch.setattr(acp, "_resolve_host", lambda h: [ipaddress.ip_address(h)] if h[0].isdigit() else [ipaddress.ip_address("93.184.216.34")])
    (slug, cfg), checks = acp.onboard_merchant(url)
    assert cfg is None and checks["store_url"]["ok"] is False
    assert store.requests_log == [], "no request may leave the server for a refused host"


def test_onboarding_refuses_hosts_resolving_to_private_addresses(onboarding_env, store, monkeypatch):
    monkeypatch.setattr(acp, "_resolve_host", lambda h: [ipaddress.ip_address("93.184.216.34"), ipaddress.ip_address("10.1.1.1")])
    (slug, cfg), checks = acp.onboard_merchant("rebind.example")
    assert cfg is None and checks["store_url"]["ok"] is False and store.requests_log == []


def test_onboarding_strips_userinfo_and_port(onboarding_env, store):
    (slug, cfg), checks = acp.onboard_merchant("https://evil@store.test:8443/shop")
    assert cfg and cfg["store_url"] == "https://store.test" and slug == "store"


def test_reonboarding_a_connected_store_needs_proof_of_control(client, onboarding_env, store):
    """Anyone can type a public store URL. Re-onboarding rotates the bearer key and replaces the Woo/Stripe keys, so
    it must require the current bearer key or working Woo REST keys."""
    hdr = {"Content-Type": "application/json"}
    r = client.post("/acp/onboard", data=json.dumps({"store_url": "https://store.test"}), headers=hdr)
    j = r.get_json()
    assert r.status_code == 200 and j["connected"] and j["slug"] == "store" and j["bearer_key"].startswith("acp_")
    first_key = j["bearer_key"]
    assert acp._runtime_merchants()["store"]["bearer_key"] == first_key
    # stranger re-onboards the same store: refused, config untouched
    r = client.post("/acp/onboard", data=json.dumps({"store_url": "store.test", "stripe_secret_key": "rk_live_attacker"}), headers=hdr)
    j = r.get_json()
    assert j["connected"] is False and j["checks"]["slug"]["ok"] is False
    assert acp._runtime_merchants()["store"]["bearer_key"] == first_key
    assert "stripe_secret_key" not in acp._runtime_merchants()["store"]
    # wrong Woo keys are not proof either
    r = client.post("/acp/onboard", data=json.dumps({"store_url": "store.test", "woo_ck": "ck_bad", "woo_cs": "cs_bad"}), headers=hdr)
    assert r.get_json()["connected"] is False
    # the owner, with the current bearer key: accepted, key rotated
    r = client.post("/acp/onboard", data=json.dumps({"store_url": "store.test", "bearer_key": first_key}), headers=hdr)
    j = r.get_json()
    assert j["connected"] and j["bearer_key"] != first_key
    # or with valid Woo REST keys (proves store admin)
    r = client.post("/acp/onboard", data=json.dumps({"store_url": "store.test", "woo_ck": "ck_test", "woo_cs": "cs_test"}), headers=hdr)
    assert r.get_json()["connected"] and acp._runtime_merchants()["store"]["woo_ck"] == "ck_test"


def test_onboarding_is_tightly_rate_limited(client, monkeypatch):
    calls = []
    monkeypatch.setattr(acp, "onboard_merchant", lambda *a, **k: calls.append(1) or (("x", None), {"store_api": {"ok": False}}))
    acp._buckets.clear()
    for i in range(acp.ONBOARD_RATE_IP):
        assert client.post("/acp/onboard", data={"store_url": "store.test"}).status_code == 200
    assert client.post("/acp/onboard", data={"store_url": "store.test"}).status_code == 429
    assert len(calls) == acp.ONBOARD_RATE_IP
    # another address is capped by the global bucket
    acp._buckets.pop("onboard:global", None)
    for i in range(acp.ONBOARD_RATE_GLOBAL):
        acp._rate_ok("onboard:global", limit=acp.ONBOARD_RATE_GLOBAL, window=acp.ONBOARD_WINDOW)
    assert client.post("/acp/onboard", data={"store_url": "store.test"}, headers={"X-Forwarded-For": "198.51.100.2"}).status_code == 429


def test_onboarding_rejects_non_object_json_without_500(client):
    for payload in ("[1,2]", '"str"', "null", '{"store_url": ["a"]}', '{"store_url": 5}'):
        r = client.post("/acp/onboard", data=payload, headers={"Content-Type": "application/json"})
        assert r.status_code == 200, payload
        assert "required" in r.get_data(as_text=True)


# ============================================================================ permalink / health
def test_permalink_empty_email_never_matches_an_order_without_email(client, api, store):
    """An order placed without buyer email has billing.email == ''. The form requires an email client-side only;
    an empty POST used to compare '' == '' and show the order (name + address)."""
    sid = api.create({"items": [{"id": "90", "quantity": 1}], "fulfillment_address": ADDRESS,
                      "buyer": {"first_name": "Ada", "last_name": "Lovelace"}})[1]["id"]
    st, body, _ = api.update(sid, {"buyer": {"first_name": "Ada", "last_name": "Lovelace", "email": ""}})
    st, body, _ = api.complete(sid)
    assert body["status"] == "completed", body
    path = body["order"]["permalink_url"].split("https://gateway.test", 1)[1]
    for data in ({"email": ""}, {"email": "   "}, {}):
        html = client.post(path, data=data).get_data(as_text=True)
        assert "Status:" not in html and "1 Main St" not in html


def test_permalink_email_guessing_is_rate_limited(client, completed_session, monkeypatch):
    _, body = completed_session
    path = body["order"]["permalink_url"].split("https://gateway.test", 1)[1]
    monkeypatch.setattr(acp, "PERMALINK_RATE", 3)
    acp._buckets.clear()
    for i in range(3):
        assert client.post(path, data={"email": f"guess{i}@example.com"}).status_code == 200
    assert client.post(path, data={"email": "ada@example.com"}).status_code == 429
    assert client.get(path).status_code == 200, "viewing the gate is not limited"


def test_health_is_rate_limited(client, store, monkeypatch):
    """Unauthenticated and opens a cart on the merchant's store per call — an amplifier without a cap."""
    monkeypatch.setattr(acp, "HEALTH_RATE", 2)
    acp._buckets.clear()
    assert client.get(f"/acp/{MERCHANT_SLUG}/health").status_code == 200
    assert client.get(f"/acp/{MERCHANT_SLUG}/health").status_code == 200
    r = client.get(f"/acp/{MERCHANT_SLUG}/health")
    assert r.status_code == 429 and r.get_json()["ok"] is False
    assert sum(1 for m, p, _ in store.requests_log if p == "/wp-json/wc/store/v1/cart") == 2


# ============================================================================ secrets at rest / persistence
class _StubFernet:
    """Reversible stand-in for cryptography.fernet.Fernet (the package is not installed in the test venv)."""
    def encrypt(self, b): return b"F" + b[::-1]
    def decrypt(self, b):
        if not b.startswith(b"F"): raise ValueError("bad token")
        return b[1:][::-1]


def test_onboarded_secrets_are_sealed_at_rest_when_a_key_is_configured(client, onboarding_env, monkeypatch):
    monkeypatch.setattr(acp, "_fernet", lambda: _StubFernet())
    r = client.post("/acp/onboard", data=json.dumps({"store_url": "store.test", "woo_ck": "ck_test", "woo_cs": "cs_test",
                                                     "stripe_secret_key": "rk_live_secret"}), headers={"Content-Type": "application/json"})
    j = r.get_json()
    assert j["connected"] and j["checks"]["storage"]["encrypted_at_rest"] is True
    raw = open(onboarding_env).read()
    for secret in ("cs_test", "rk_live_secret", j["bearer_key"]):
        assert secret not in raw, f"{secret[:6]}… must not be on disk in plaintext"
    m = acp.merchants()["store"]
    assert m["woo_cs"] == "cs_test" and m["stripe_secret_key"] == "rk_live_secret" and m["bearer_key"] == j["bearer_key"]
    # the sealed bearer key still authenticates
    r = client.post("/acp/store/checkout_sessions", data=json.dumps({"items": [{"id": "90", "quantity": 1}]}),
                    headers={"Authorization": f"Bearer {j['bearer_key']}", "Content-Type": "application/json"})
    assert r.status_code == 201
    # key rotated/lost: ciphertext is never handed out as a credential
    monkeypatch.setattr(acp, "_fernet", lambda: None)
    m = acp.merchants()["store"]
    assert m["stripe_secret_key"] is None and m["bearer_key"] is None


def test_onboarding_without_secrets_key_says_so(client, onboarding_env, monkeypatch):
    monkeypatch.setattr(acp, "_fernet", lambda: None)
    r = client.post("/acp/onboard", data=json.dumps({"store_url": "store.test"}), headers={"Content-Type": "application/json"})
    st = r.get_json()["checks"]["storage"]
    assert st["encrypted_at_rest"] is False and "plaintext" in st["note"]


def test_runtime_merchant_persist_is_atomic_and_keeps_other_data(onboarding_env, monkeypatch):
    with open(onboarding_env, "w") as f:
        json.dump({"leads": [{"domain": "x"}], "acp_merchants": {"old": {"store_url": "https://old.test", "bearer_key": "b"}}}, f)
    acp._persist_runtime_merchant("new", {"store_url": "https://new.test", "bearer_key": "n"})
    d = json.load(open(onboarding_env))
    assert d["leads"] == [{"domain": "x"}] and set(d["acp_merchants"]) == {"old", "new"}
    import glob, os
    assert not glob.glob(onboarding_env + ".*.tmp"), "temp file replaced, not left behind"
    # a corrupt data file is not fatal
    open(onboarding_env, "w").write("{not json")
    acp._persist_runtime_merchant("again", {"store_url": "https://again.test", "bearer_key": "a"})
    assert "again" in json.load(open(onboarding_env))["acp_merchants"]
