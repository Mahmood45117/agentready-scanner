"""
Test harness for the Agentic Checkout gateway (scanner/acp.py).

No network: `acp.requests` is replaced with FakeRequests, which routes every HTTP call to FakeWooStore — an
in-memory model of the WooCommerce Store API (cart quoting) and REST v3 (orders), shaped after a real
linealprints.com response (item 90 @ 3900 minor units, flat_rate:1 @ 690, tax 0, USD/2).

Keeping the real `acp.Woo` class in the loop means the URL building, Cart-Token handling, add-item error
mapping ("stock" -> out_of_stock) and the two-step pending->processing order creation are all under test.
"""
import json
import os
import sys
import tempfile
import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from requests.structures import CaseInsensitiveDict

# --------------------------------------------------------------------------- environment BEFORE importing app
HERE = os.path.dirname(os.path.abspath(__file__))
SCANNER_DIR = os.path.dirname(HERE)
if SCANNER_DIR not in sys.path:
    sys.path.insert(0, SCANNER_DIR)

_IMPORT_TMP = tempfile.mkdtemp(prefix="acp-tests-")
BEARER = "test-bearer"
MERCHANT_SLUG = "t"
BASE_MERCHANT = {
    "store_url": "https://store.test",
    "bearer_key": BEARER,
    "tos_url": "https://store.test/terms/",
    "privacy_url": "https://store.test/privacy/",
    "woo_ck": "ck_test",
    "woo_cs": "cs_test",
    "seller_name": "Test Prints",
    "shipping": {"flat_rate:1": {"carrier": "USPS", "min_days": 3, "max_days": 7}},
}

os.environ["ACP_MOCK_PAYMENTS"] = "1"
os.environ["ACP_DB"] = os.path.join(_IMPORT_TMP, "import.sqlite")
os.environ["DATA_FILE"] = os.path.join(_IMPORT_TMP, "cani_data.json")
os.environ["ACP_MERCHANTS"] = json.dumps({MERCHANT_SLUG: BASE_MERCHANT})
os.environ["ACP_PERMALINK_SECRET"] = "test-permalink-secret"
os.environ["ACP_PUBLIC_BASE"] = "https://gateway.test"
os.environ.pop("ACP_REQUIRE_SIGNATURE", None)

import app as app_module  # noqa: E402  (env must be set first)
import acp  # noqa: E402

assert "acp" in app_module.app.blueprints, "acp blueprint failed to register on app.app (see stderr traceback)"


# --------------------------------------------------------------------------- fake HTTP layer
class StoreDown(Exception):
    """Raised by the fake when a call is configured to fail (simulates ConnectionError/timeouts)."""


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None, text=None):
        self.status_code = status
        self._body = body
        self.headers = CaseInsensitiveDict(headers or {})
        if body is not None and "content-type" not in self.headers:
            self.headers["Content-Type"] = "application/json; charset=UTF-8"
        self.text = text if text is not None else (json.dumps(body) if body is not None else "")

    def json(self):
        if self._body is None:
            raise ValueError("no json body")
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeWooStore:
    """In-memory WooCommerce. Toggles:
       prices[id]        -> override unit price in minor units (quote drift)
       out_of_stock_ids  -> ids that reject add-item with an out-of-stock message
       fail_create       -> POST /wc/v3/orders raises StoreDown
       fail_cart         -> GET /wc/store/v1/cart (new cart) raises StoreDown
       webhook_status    -> status code returned to outbound OpenAI webhook posts
    """
    CURRENCY = {"currency_code": "USD", "currency_symbol": "$", "currency_minor_unit": 2,
                "currency_decimal_separator": ".", "currency_thousand_separator": ",",
                "currency_prefix": "$", "currency_suffix": ""}
    SHIP_RATE = {"rate_id": "flat_rate:1", "name": "Flat rate", "description": "", "delivery_time": "",
                 "price": "690", "taxes": "0", "instance_id": 1, "method_id": "flat_rate", "meta_data": []}

    def __init__(self):
        self.catalog = {
            90: {"name": "Series 01 — No. 3 (16×24)", "price": 3900, "stock": 5},
            91: {"name": "Series 01 — No. 4 (20×30)", "price": 4900, "stock": 5},
        }
        self.prices = {}
        self.out_of_stock_ids = set()
        self.fail_create = False
        self.fail_cart = False
        self.webhook_status = 200
        self.carts = {}
        self.orders = {}
        self.next_order_id = 4242
        self.requests_log = []
        self.webhook_posts = []
        self.cron_pokes = 0
        self.stripe_events = {}     # evt_id -> event JSON served by GET api.stripe.com/v1/events/<id>
        self.stripe_calls = []      # (method, path, auth) for every api.stripe.com call
        self.stripe_pis = []        # PaymentIntent creations (data + headers) when MOCK_PAY is off

    # ---- cart model -> Store API JSON
    def _price(self, pid):
        return self.prices.get(pid, self.catalog[pid]["price"])

    def _cart_json(self, cart):
        items, total_items = [], 0
        for it in cart["items"]:
            unit = self._price(it["id"])
            line = unit * it["quantity"]
            total_items += line
            items.append({
                "key": f"k{it['id']}", "id": it["id"], "type": "simple", "quantity": it["quantity"],
                "name": self.catalog[it["id"]]["name"], "sku": f"SKU-{it['id']}",
                "prices": dict(self.CURRENCY, price=str(unit), regular_price=str(unit), sale_price=str(unit)),
                "totals": dict(self.CURRENCY, line_subtotal=str(line), line_subtotal_tax="0",
                               line_total=str(line), line_total_tax="0"),
                "item_data": [], "variation": [],
            })
        has_addr = cart["address"] is not None
        selected = has_addr and cart["rate"] == self.SHIP_RATE["rate_id"]
        rate = dict(self.SHIP_RATE, selected=selected)
        shipping = int(self.SHIP_RATE["price"]) if selected else 0
        return {
            "items": items,
            "coupons": [], "fees": [], "needs_shipping": True, "needs_payment": True, "has_calculated_shipping": has_addr,
            "shipping_address": cart["address"] or {}, "billing_address": cart["billing"] or {},
            "shipping_rates": [{"package_id": 0, "name": "Shipping", "destination": cart["address"] or {},
                                "items": [{"key": f"k{i['id']}", "name": self.catalog[i["id"]]["name"], "quantity": i["quantity"]}
                                          for i in cart["items"]],
                                "shipping_rates": [rate]}],
            "totals": dict(self.CURRENCY, total_items=str(total_items), total_items_tax="0",
                           total_fees="0", total_fees_tax="0", total_discount="0", total_discount_tax="0",
                           total_shipping=str(shipping), total_shipping_tax="0", total_price=str(total_items + shipping),
                           total_tax="0", tax_lines=[]),
            "errors": [], "payment_requirements": ["products", "shipping"], "extensions": {},
        }

    # ---- router
    def handle(self, method, url, json_body=None, data=None, headers=None, params=None, auth=None):
        u = urlparse(url)
        path, q = u.path, parse_qs(u.query)
        headers = headers or {}
        self.requests_log.append((method, path, json_body))
        if path == "/wp-json/wc/store/v1/cart" and method == "GET":
            tok = headers.get("Cart-Token")
            if not tok:
                if self.fail_cart:
                    raise StoreDown("store unreachable")
                tok = "ct_" + uuid.uuid4().hex[:12]
                self.carts[tok] = {"items": [], "address": None, "billing": None, "rate": None}
                return FakeResponse(200, self._cart_json(self.carts[tok]), {"Cart-Token": tok})
            return FakeResponse(200, self._cart_json(self.carts[tok]))
        if path.startswith("/wp-json/wc/store/v1/cart/"):
            cart = self.carts[headers["Cart-Token"]]
            action = path.rsplit("/", 1)[1]
            if action == "add-item":
                pid, qty = json_body["id"], json_body["quantity"]
                if pid not in self.catalog:
                    return FakeResponse(400, {"code": "woocommerce_rest_product_invalid_id", "message": "Invalid product ID.",
                                              "data": {"status": 400}})
                name = self.catalog[pid]["name"]
                if pid in self.out_of_stock_ids or self.catalog[pid]["stock"] == 0:
                    return FakeResponse(400, {"code": "woocommerce_rest_cart_product_no_stock",
                                              "message": f"You cannot add \"{name}\" to the cart because the product is out of stock.",
                                              "data": {"status": 400}})
                if qty > self.catalog[pid]["stock"]:
                    return FakeResponse(400, {"code": "woocommerce_rest_cart_item_error",
                                              "message": f"You cannot add that amount of \"{name}\" to the cart because there is "
                                                         f"not enough stock ({self.catalog[pid]['stock']} remaining).",
                                              "data": {"status": 400}})
                for it in cart["items"]:
                    if it["id"] == pid:
                        it["quantity"] += qty
                        break
                else:
                    cart["items"].append({"id": pid, "quantity": qty})
                return FakeResponse(201, self._cart_json(cart))
            if action == "update-customer":
                cart["address"] = json_body.get("shipping_address")
                cart["billing"] = json_body.get("billing_address")
                if cart["address"] and not cart["address"].get("country"):
                    return FakeResponse(400, {"code": "woocommerce_rest_invalid_address", "message": "Country is required"})
                cart["rate"] = self.SHIP_RATE["rate_id"]   # Woo auto-selects the first available rate
                return FakeResponse(200, self._cart_json(cart))
            if action == "select-shipping-rate":
                if json_body["rate_id"] != self.SHIP_RATE["rate_id"]:
                    return FakeResponse(400, {"code": "woocommerce_rest_cart_shipping_rate_error",
                                              "message": "Invalid shipping rate."})
                cart["rate"] = json_body["rate_id"]
                return FakeResponse(200, self._cart_json(cart))
            raise AssertionError(f"unexpected Store API call {method} {path}")
        if path == "/wp-json/wc/v3/orders" and method == "POST":
            assert auth == ("ck_test", "cs_test"), "REST v3 must use merchant keys"
            if self.fail_create:
                raise StoreDown("store unreachable")
            oid = self.next_order_id
            self.next_order_id += 1
            body = json_body
            lines = []
            for li in body["line_items"]:
                pid = li.get("variation_id") or li["product_id"]
                lines.append({"id": 10 + len(lines), "name": self.catalog[pid]["name"], "product_id": pid,
                              "quantity": li["quantity"], "total": f"{self._price(pid) * li['quantity'] / 100:.2f}"})
            shipping_total = sum(float(s["total"]) for s in body.get("shipping_lines", []))
            order = {
                "id": oid, "status": body.get("status", "pending"), "currency": body.get("currency", "USD"),
                "date_created": "2026-09-02T10:00:00", "date_modified": "2026-09-02T10:00:00",
                "billing": body.get("billing", {}), "shipping": body.get("shipping", {}),
                "payment_method": body.get("payment_method"), "transaction_id": body.get("transaction_id", ""),
                "set_paid": body.get("set_paid", False), "line_items": lines, "shipping_lines": body.get("shipping_lines", []),
                "shipping_total": f"{shipping_total:.2f}",
                "total": f"{sum(float(l['total']) for l in lines) + shipping_total:.2f}",
                "meta_data": [dict(md, id=i) for i, md in enumerate(body.get("meta_data", []))],
                "customer_note": body.get("customer_note", ""), "refunds": [],
            }
            self.orders[oid] = order
            return FakeResponse(201, order)
        if path.endswith("/refunds") and path.startswith("/wp-json/wc/v3/orders/") and method == "POST":
            oid = int(path.split("/")[-2])
            ref = dict(json_body, id=700 + len(self.orders[oid]["refunds"]))
            self.orders[oid]["refunds"].append(ref)
            return FakeResponse(201, ref)
        if path.startswith("/wp-json/wc/v3/orders/") and method == "PUT":
            oid = int(path.rsplit("/", 1)[1])
            order = self.orders[oid]
            order.update({k: v for k, v in json_body.items()})
            order["date_modified"] = "2026-09-02T10:00:05"
            return FakeResponse(200, order)
        if path == "/wp-json/wc/store/v1/products" and method == "GET":
            return FakeResponse(200, [{"id": pid, "is_purchasable": True, "is_in_stock": p["stock"] > 0, "variations": []}
                                      for pid, p in self.catalog.items()])
        if path == "/wp-json/wp/v2/pages" and method == "GET":
            return FakeResponse(200, [])
        if path == "/wp-json/wc/v3/orders" and method == "GET":
            if auth != ("ck_test", "cs_test"):
                return FakeResponse(401, {"code": "woocommerce_rest_cannot_view", "message": "Sorry, you cannot list resources."})
            needle = (params or {}).get("search") or (q.get("search") or [""])[0]
            found = [o for o in self.orders.values()
                     if any(md.get("value") == needle for md in o["meta_data"])]
            return FakeResponse(200, found)
        if path.startswith("/wp-json/wc/v3/orders/") and method == "GET":
            oid = int(path.rsplit("/", 1)[1])
            if oid in self.orders:
                return FakeResponse(200, self.orders[oid])
            return FakeResponse(404, {"code": "woocommerce_rest_shop_order_invalid_id", "message": "Invalid ID."})
        if path == "/wp-cron.php":
            self.cron_pokes += 1
            return FakeResponse(200, text="")
        if u.netloc == "openai-webhooks.test":
            self.webhook_posts.append({"url": url, "headers": headers, "body": data})
            return FakeResponse(self.webhook_status, {"ok": True})
        if u.netloc == "api.stripe.com":
            self.stripe_calls.append((method, path, auth))
            if method == "GET" and path.startswith("/v1/events/"):
                ev = self.stripe_events.get(path.rsplit("/", 1)[1])
                return FakeResponse(200, ev) if ev else FakeResponse(404, {"error": {"message": "No such event"}})
            if method == "GET" and path == "/v1/account":
                return FakeResponse(200, {"id": "acct_test", "country": "US", "charges_enabled": True})
            if method == "POST" and path == "/v1/payment_intents":
                self.stripe_pis.append({"data": data, "headers": headers})
                return FakeResponse(200, {"id": f"pi_fake_{len(self.stripe_pis)}", "status": "succeeded"})
            raise AssertionError(f"unexpected Stripe call {method} {path}")
        raise AssertionError(f"unexpected HTTP call {method} {url}")

    # ---- helpers for tests: mutate the store the way WooCommerce would, then the webhook only *notifies*
    def change_order(self, oid, **fields):
        o = self.orders[oid]
        o.update(fields)
        o["date_modified"] = f"2026-09-02T{12 + len(self.requests_log) % 12:02d}:{len(self.requests_log) % 60:02d}:00"
        return o


class FakeSession:
    def __init__(self, store):
        self.store = store
        self.headers = {}

    def _merge(self, headers):
        h = dict(self.headers)
        h.update(headers or {})
        return h

    def get(self, url, headers=None, params=None, timeout=None, **kw):
        return self.store.handle("GET", url, headers=self._merge(headers), params=params)

    def post(self, url, json=None, data=None, headers=None, timeout=None, **kw):
        return self.store.handle("POST", url, json_body=json, data=data, headers=self._merge(headers))


class FakeRequests:
    """Drop-in for the `requests` module surface acp.py uses."""
    def __init__(self, store):
        self.store = store

    def Session(self):
        return FakeSession(self.store)

    def get(self, url, headers=None, params=None, auth=None, timeout=None, **kw):
        return self.store.handle("GET", url, headers=headers, params=params, auth=auth)

    def post(self, url, json=None, data=None, headers=None, auth=None, timeout=None, **kw):
        return self.store.handle("POST", url, json_body=json, data=data, headers=headers, auth=auth)

    def put(self, url, json=None, headers=None, auth=None, timeout=None, **kw):
        return self.store.handle("PUT", url, json_body=json, headers=headers, auth=auth)


# --------------------------------------------------------------------------- fixtures
@pytest.fixture
def store(monkeypatch):
    s = FakeWooStore()
    monkeypatch.setattr(acp, "requests", FakeRequests(s))
    return s


@pytest.fixture
def merchant(monkeypatch, tmp_path):
    """Fresh merchant config + fresh sqlite per test. Returns the live merchant dict (mutate to add secrets)."""
    cfg = json.loads(json.dumps(BASE_MERCHANT))
    monkeypatch.setenv("ACP_MERCHANTS", json.dumps({MERCHANT_SLUG: cfg}))
    monkeypatch.setattr(acp, "_MERCHANTS", None)
    monkeypatch.setattr(acp, "DB_PATH", str(tmp_path / "acp.sqlite"))
    monkeypatch.setattr(acp, "MOCK_PAY", True)
    monkeypatch.setattr(acp, "_kick", lambda: None)   # never start the background worker; tests call _deliver directly
    # per-(merchant, ip) rate limiter is a module global; give every test a clean bucket and a generous limit
    monkeypatch.setattr(acp, "RATE_LIMIT", 10_000, raising=False)
    monkeypatch.setattr(acp, "ENFORCE_IP", False, raising=False)
    if hasattr(acp, "_buckets"):
        acp._buckets.clear()
    acp.init_db()
    return acp.merchants()[MERCHANT_SLUG]


@pytest.fixture
def client(store, merchant):
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


class Api:
    """Thin helper: sends spec headers, returns (status, body_dict, headers)."""
    def __init__(self, client):
        self.c = client
        self.base = f"/acp/{MERCHANT_SLUG}"

    def headers(self, key=None, **extra):
        h = {"Authorization": f"Bearer {BEARER}", "API-Version": acp.API_VERSION, "Content-Type": "application/json",
             "Request-Id": "req_" + uuid.uuid4().hex[:8], "Idempotency-Key": key or ("idem_" + uuid.uuid4().hex[:8])}
        for k, v in extra.items():
            hk = k.replace("_", "-")
            if v is None:
                h.pop(hk, None)
            else:
                h[hk] = v
        return h

    def _call(self, method, path, body=None, key=None, **extra):
        fn = getattr(self.c, method)
        kwargs = {"headers": self.headers(key, **extra)}
        if body is not None:
            kwargs["data"] = json.dumps(body)
        r = fn(path, **kwargs)
        try:
            j = r.get_json(force=True)
        except Exception:
            j = None
        return r.status_code, j, r.headers

    def create(self, body=None, key=None, **extra):
        body = {"items": [{"id": "90", "quantity": 1}]} if body is None else body
        return self._call("post", f"{self.base}/checkout_sessions", body, key, **extra)

    def update(self, sid, body, key=None, **extra):
        return self._call("post", f"{self.base}/checkout_sessions/{sid}", body, key, **extra)

    def get(self, sid, **extra):
        return self._call("get", f"{self.base}/checkout_sessions/{sid}", None, None, **extra)

    def cancel(self, sid, key=None, **extra):
        return self._call("post", f"{self.base}/checkout_sessions/{sid}/cancel", {}, key, **extra)

    def complete(self, sid, token="spt_test_ok", key=None, body=None, **extra):
        body = body if body is not None else {"payment_data": {"token": token, "provider": "stripe"}}
        return self._call("post", f"{self.base}/checkout_sessions/{sid}/complete", body, key, **extra)


ADDRESS = {"name": "Ada Lovelace", "line_one": "1 Main St", "line_two": "", "city": "Boston", "state": "MA",
           "country": "US", "postal_code": "02101"}
BUYER = {"first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com", "phone_number": "+15550001111"}


@pytest.fixture
def api(client):
    return Api(client)


@pytest.fixture
def ready_session(api):
    """A session with items + address + buyer, i.e. status ready_for_payment. Returns the session id."""
    st, body, _ = api.create()
    assert st == 201, body
    sid = body["id"]
    st, body, _ = api.update(sid, {"fulfillment_address": ADDRESS, "buyer": BUYER})
    assert st == 200 and body["status"] == "ready_for_payment", body
    return sid


@pytest.fixture
def completed_session(api, ready_session):
    """A completed session whose Woo order is 4242. Returns (session_id, complete-response body)."""
    st, body, _ = api.complete(ready_session)
    assert st == 200 and body["status"] == "completed", body
    return ready_session, body


def db_rows(sql, *args):
    with acp._db() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


@pytest.fixture
def onboarding_env(monkeypatch, store, merchant):
    """Onboarding without network: public DNS stubbed, policy discovery + LLM stubbed, fresh DATA_FILE."""
    import ipaddress
    monkeypatch.setattr(acp, "_openai_nets", lambda block=False: [])
    monkeypatch.setattr(acp, "_resolve_host", lambda h: [ipaddress.ip_address("93.184.216.34")])
    monkeypatch.setattr(acp, "_store_policy_text", lambda *a, **k: "")
    monkeypatch.setattr(acp, "_llm_shipping_map", lambda *a, **k: {})
    import feed_engine
    monkeypatch.setattr(feed_engine, "discover_policies", lambda d: {"seller_privacy_policy": "https://x/p", "seller_tos": "https://x/t"})
    df = os.path.join(tempfile.mkdtemp(prefix="acp-onboard-"), "data.json")
    monkeypatch.setenv("DATA_FILE", df)
    return df
