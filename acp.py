"""
acp.py — Agentic Checkout gateway (OpenAI Agentic Commerce Protocol) for non-Shopify stores.

Hosts the five merchant endpoints ChatGPT Instant Checkout calls, in front of a WooCommerce store:

  POST /acp/<merchant>/checkout_sessions                 create
  POST /acp/<merchant>/checkout_sessions/<id>            update
  POST /acp/<merchant>/checkout_sessions/<id>/complete   charge + create order
  POST /acp/<merchant>/checkout_sessions/<id>/cancel
  GET  /acp/<merchant>/checkout_sessions/<id>

Quoting  : WooCommerce Store API (public, Cart-Token) — prices, shipping rates, tax, totals in minor units.
Payment  : Stripe Shared Payment Token (spt_…) forwarded by OpenAI -> one PaymentIntent on the merchant's Stripe.
Orders   : WooCommerce REST /wc/v3/orders (merchant keys), set_paid, transaction_id = pi_….
Webhooks : order_created / order_updated -> OpenAI (HMAC-SHA256, outbox with retry).
           Woo order.updated -> /acp/<merchant>/webhooks/woo (status -> order_updated).
Permalink: /acp/o/<token> — email-gated order page (Woo has no login-free order URL).

Spec: developers.openai.com/commerce/specs/checkout (API-Version 2025-09-12). See ../acp-gateway-design.md.
Card data never touches this service (PCI: only spt_/pi_ ids and last4).
"""
import base64, hashlib, hmac, json, os, secrets, sqlite3, threading, time, uuid
from datetime import datetime, timedelta, timezone

import requests
from flask import Blueprint, Response, jsonify, request, render_template_string

bp = Blueprint("acp", __name__)
API_VERSION = "2025-09-12"
UA = {"User-Agent": "CanAIShopYou-ACP/1.0 (+https://canaishopyou.com)"}
DB_PATH = os.environ.get("ACP_DB", "/tmp/acp.sqlite")
SESSION_TTL = 60 * 60          # seconds a quote stays valid
IDEMP_TTL = 24 * 60 * 60
TS_WINDOW = 300                # ±5 min Timestamp tolerance
MOCK_PAY = os.environ.get("ACP_MOCK_PAYMENTS") == "1"   # local testing without Stripe

# ----------------------------------------------------------------------------- merchants
_MERCHANTS = None

def merchants():
    """{slug: config}. Source: env ACP_MERCHANTS (JSON) or acp_merchants.json next to this file (gitignored).
    Required per merchant: store_url, bearer_key, tos_url, privacy_url. For /complete: woo_ck, woo_cs, stripe_secret_key.
    Optional: policies_url, currency (default from cart), seller_name, openai_webhook_url, openai_webhook_key,
              woo_webhook_secret, shipping: {"<woo rate_id or method_id>": {carrier, min_days, max_days, title}}"""
    global _MERCHANTS
    if _MERCHANTS is None:
        raw = os.environ.get("ACP_MERCHANTS")
        if not raw:
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "acp_merchants.json")
            raw = open(p).read() if os.path.exists(p) else "{}"
        _MERCHANTS = json.loads(raw)
    return _MERCHANTS

def _m(slug):
    return merchants().get(slug)

# ----------------------------------------------------------------------------- storage
_db_lock = threading.Lock()

def _db():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with _db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, merchant TEXT, status TEXT, cart_token TEXT,
            items TEXT, buyer TEXT, address TEXT, option_id TEXT, quote TEXT, quote_hash TEXT,
            order_id TEXT, created REAL, updated REAL);
        CREATE TABLE IF NOT EXISTS idem (merchant TEXT, key TEXT, endpoint TEXT, body_hash TEXT, status INTEGER,
            response TEXT, created REAL, PRIMARY KEY (merchant, key, endpoint));
        CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, merchant TEXT, session_id TEXT, woo_order_id TEXT,
            stripe_pi TEXT, amount INTEGER, currency TEXT, status TEXT, email TEXT, refunded INTEGER DEFAULT 0, created REAL);
        CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, merchant TEXT, event TEXT, payload TEXT,
            attempts INTEGER DEFAULT 0, next_at REAL, delivered REAL);
        CREATE TABLE IF NOT EXISTS inbound (source TEXT, ext_id TEXT, received REAL, PRIMARY KEY (source, ext_id));
        """)

init_db()

def _now():
    return time.time()

def _rfc3339(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def _sid():
    return "cs_" + secrets.token_hex(12)

def _j(x):
    return json.dumps(x, separators=(",", ":"), sort_keys=True)

# ----------------------------------------------------------------------------- errors / responses
class ACPError(Exception):
    def __init__(self, status, code, message, param=None, etype="invalid_request"):
        self.status, self.code, self.message, self.param, self.etype = status, code, message, param, etype

def _echo_headers(resp):
    for h in ("Idempotency-Key", "Request-Id"):
        v = request.headers.get(h)
        if v:
            resp.headers[h] = v
    resp.headers["API-Version"] = API_VERSION
    return resp

def _error(e):
    body = {"type": e.etype, "code": e.code, "message": e.message}
    if e.param:
        body["param"] = e.param
    return _echo_headers(jsonify(body)), e.status

@bp.errorhandler(ACPError)
def _on_acp_error(e):
    return _error(e)

# ----------------------------------------------------------------------------- request pipeline
def _auth(slug):
    m = _m(slug)
    if not m:
        raise ACPError(404, "merchant_not_found", "unknown merchant")
    auth = request.headers.get("Authorization", "")
    tok = auth[7:] if auth.startswith("Bearer ") else ""
    if not tok or not hmac.compare_digest(tok, m.get("bearer_key", "")):
        raise ACPError(401, "unauthorized", "invalid bearer token", etype="invalid_request")
    ts = request.headers.get("Timestamp")
    if ts:
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            if abs(_now() - t) > TS_WINDOW:
                raise ACPError(400, "invalid", "Timestamp outside allowed window", param="$.headers.Timestamp")
        except ValueError:
            raise ACPError(400, "invalid", "Timestamp not RFC 3339", param="$.headers.Timestamp")
    ver = request.headers.get("API-Version")
    if ver and ver != API_VERSION:
        raise ACPError(400, "invalid", f"unsupported API-Version {ver}; this endpoint speaks {API_VERSION}",
                       param="$.headers.API-Version")
    # Signature: algorithm/key exchange not published by OpenAI yet. Verify HMAC-SHA256 over raw body when a key is
    # configured; fail closed only if ACP_REQUIRE_SIGNATURE=1.
    sig, key = request.headers.get("Signature"), m.get("acp_signature_key")
    if key and sig:
        want = base64.b64encode(hmac.new(key.encode(), request.get_data(), hashlib.sha256).digest()).decode()
        if not hmac.compare_digest(sig, want):
            raise ACPError(401, "invalid", "bad request signature", param="$.headers.Signature")
    elif os.environ.get("ACP_REQUIRE_SIGNATURE") == "1":
        raise ACPError(401, "missing", "Signature header required", param="$.headers.Signature")
    return m

def _idem_start(slug, endpoint):
    """Returns (replay_response or None, key, body_hash)."""
    key = request.headers.get("Idempotency-Key")
    if not key:
        return None, None, None
    bh = hashlib.sha256(request.get_data()).hexdigest()
    with _db_lock, _db() as c:
        c.execute("DELETE FROM idem WHERE created < ?", (_now() - IDEMP_TTL,))
        row = c.execute("SELECT * FROM idem WHERE merchant=? AND key=? AND endpoint=?", (slug, key, endpoint)).fetchone()
        if row:
            if row["body_hash"] != bh:
                raise ACPError(409, "idempotency_conflict", "Idempotency-Key reused with a different body",
                               etype="request_not_idempotent")
            if row["status"] is None:   # in flight
                r = _echo_headers(jsonify({"type": "processing", "code": "in_progress", "message": "request in progress"}))
                r.headers["Retry-After"] = "2"
                return (r, 409), key, bh
            r = _echo_headers(Response(row["response"], mimetype="application/json"))
            r.headers["Idempotent-Replayed"] = "true"
            return (r, row["status"]), key, bh
        c.execute("INSERT INTO idem (merchant,key,endpoint,body_hash,status,response,created) VALUES (?,?,?,?,NULL,NULL,?)",
                  (slug, key, endpoint, bh, _now()))
    return None, key, bh

def _idem_finish(slug, endpoint, key, status, body_json):
    if not key:
        return
    with _db_lock, _db() as c:
        c.execute("UPDATE idem SET status=?, response=? WHERE merchant=? AND key=? AND endpoint=?",
                  (status, body_json, slug, key, endpoint))

def _idem_abort(slug, endpoint, key):
    if not key:
        return
    with _db_lock, _db() as c:
        c.execute("DELETE FROM idem WHERE merchant=? AND key=? AND endpoint=?", (slug, key, endpoint))

def _reply(slug, endpoint, key, status, body):
    txt = _j(body)
    _idem_finish(slug, endpoint, key, status, txt)
    return _echo_headers(Response(txt, mimetype="application/json")), status

# ----------------------------------------------------------------------------- WooCommerce Store API (quoting)
class Woo:
    def __init__(self, m):
        self.base = m["store_url"].rstrip("/")
        self.m = m
        self.s = requests.Session()
        self.s.headers.update(UA)

    def _url(self, p):
        return f"{self.base}/wp-json/wc/store/v1{p}"

    def new_cart(self):
        r = self.s.get(self._url("/cart"), timeout=20)
        r.raise_for_status()
        tok = r.headers.get("Cart-Token")
        if not tok:
            raise ACPError(502, "invalid", "store did not issue a Cart-Token (Store API disabled?)")
        return tok

    def _h(self, tok):
        return {"Cart-Token": tok}

    def add_items(self, tok, items):
        cart = None
        for it in items:
            r = self.s.post(self._url("/cart/add-item"), json={"id": int(it["id"]), "quantity": int(it["quantity"])},
                            headers=self._h(tok), timeout=20)
            if r.status_code not in (200, 201):
                msg = (r.json().get("message") if r.headers.get("content-type", "").startswith("application/json") else r.text)[:200]
                code = "out_of_stock" if "stock" in (msg or "").lower() else "invalid"
                raise ACPError(400, code, msg or "item rejected by store", param=f"$.items[?(@.id=='{it['id']}')]")
            cart = r.json()
        return cart

    def set_address(self, tok, addr, buyer):
        a = {
            "first_name": (buyer or {}).get("first_name", "") or addr.get("name", "").split(" ")[0],
            "last_name": (buyer or {}).get("last_name", "") or " ".join(addr.get("name", "").split(" ")[1:]),
            "address_1": addr.get("line_one", ""), "address_2": addr.get("line_two", "") or "",
            "city": addr.get("city", ""), "state": addr.get("state", ""), "postcode": addr.get("postal_code", ""),
            "country": addr.get("country", "US"),
        }
        body = {"shipping_address": a, "billing_address": dict(a, email=(buyer or {}).get("email", "") or "",
                                                              phone=(buyer or {}).get("phone_number", "") or "")}
        r = self.s.post(self._url("/cart/update-customer"), json=body, headers=self._h(tok), timeout=20)
        if r.status_code != 200:
            raise ACPError(400, "invalid", "store rejected the address", param="$.fulfillment_address")
        return r.json()

    def select_rate(self, tok, rate_id, package_id=0):
        r = self.s.post(self._url("/cart/select-shipping-rate"), json={"package_id": package_id, "rate_id": rate_id},
                        headers=self._h(tok), timeout=20)
        if r.status_code != 200:
            raise ACPError(400, "invalid", "store rejected the shipping option", param="$.fulfillment_option_id")
        return r.json()

    def get_cart(self, tok):
        r = self.s.get(self._url("/cart"), headers=self._h(tok), timeout=20)
        r.raise_for_status()
        return r.json()

    # --- REST v3 (merchant keys) ---
    def create_order(self, sess, quote, buyer, addr, pi_id, option):
        auth = (self.m["woo_ck"], self.m["woo_cs"])
        name = (addr or {}).get("name", "") or f"{(buyer or {}).get('first_name','')} {(buyer or {}).get('last_name','')}".strip()
        first, last = (name.split(" ")[0], " ".join(name.split(" ")[1:])) if name else ("", "")
        a = {"first_name": first, "last_name": last, "address_1": (addr or {}).get("line_one", ""),
             "address_2": (addr or {}).get("line_two", "") or "", "city": (addr or {}).get("city", ""),
             "state": (addr or {}).get("state", ""), "postcode": (addr or {}).get("postal_code", ""),
             "country": (addr or {}).get("country", "US")}
        line_items = []
        for it in quote["_cart_items"]:
            li = {"quantity": it["quantity"]}
            if it["type"] == "variation":
                li["variation_id"] = it["id"]; li["product_id"] = it.get("parent_id") or it["id"]
            else:
                li["product_id"] = it["id"]
            line_items.append(li)
        body = {
            "status": "processing", "set_paid": True, "payment_method": "stripe",
            "payment_method_title": "ChatGPT Instant Checkout (Stripe)", "transaction_id": pi_id,
            "currency": quote["currency"].upper(),
            "billing": dict(a, email=(buyer or {}).get("email", ""), phone=(buyer or {}).get("phone_number", "") or ""),
            "shipping": a, "line_items": line_items,
            "shipping_lines": [{"method_id": option["_method_id"], "method_title": option["title"],
                                "total": f"{option['total'] / 100:.2f}"}] if option else [],
            "customer_note": "Placed via ChatGPT Instant Checkout",
            "meta_data": [{"key": "acp_checkout_session_id", "value": sess["id"]},
                          {"key": "stripe_payment_intent", "value": pi_id},
                          {"key": "_acp_gateway", "value": "canaishopyou"}],
        }
        # two steps on purpose: create as pending, then flip to processing, so the store's
        # "status -> processing" hooks fire (fulfilment integrations like Printify listen on that transition)
        body.update({"status": "pending", "set_paid": False})
        r = requests.post(f"{self.base}/wp-json/wc/v3/orders", json=body, auth=auth, headers=UA, timeout=30)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"woo order create failed {r.status_code}: {r.text[:300]}")
        o = r.json()
        r2 = requests.put(f"{self.base}/wp-json/wc/v3/orders/{o['id']}", json={"status": "processing", "set_paid": True,
                          "transaction_id": pi_id}, auth=auth, headers=UA, timeout=30)
        # Woo delivers webhooks (e.g. to Printify) via Action Scheduler on WP-Cron, which only runs on page traffic —
        # observed ~1h delay on a quiet store. Poke cron so fulfilment hears about the order now (best effort).
        try:
            requests.get(f"{self.base}/wp-cron.php?doing_wp_cron", headers=UA, timeout=10)
        except Exception:
            pass
        return r2.json() if r2.status_code == 200 else o

    def find_order_by_session(self, session_id):
        auth = (self.m["woo_ck"], self.m["woo_cs"])
        r = requests.get(f"{self.base}/wp-json/wc/v3/orders", params={"search": session_id, "per_page": 5},
                         auth=auth, headers=UA, timeout=30)
        if r.status_code == 200:
            for o in r.json():
                if any(md.get("key") == "acp_checkout_session_id" and md.get("value") == session_id for md in o.get("meta_data", [])):
                    return o
        return None

    def get_order(self, order_id):
        auth = (self.m["woo_ck"], self.m["woo_cs"])
        r = requests.get(f"{self.base}/wp-json/wc/v3/orders/{order_id}", auth=auth, headers=UA, timeout=30)
        return r.json() if r.status_code == 200 else None

# ----------------------------------------------------------------------------- quote -> spec objects
def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0

def build_quote(m, cart, option_id=None):
    """Map a Woo Store API cart to the spec's line_items / fulfillment_options / totals."""
    cur = (cart["totals"].get("currency_code") or m.get("currency") or "USD").lower()
    line_items, cart_items = [], []
    for it in cart["items"]:
        unit = _int(it["prices"]["price"])
        qty = int(it["quantity"])
        base = unit * qty
        sub = _int(it["totals"]["line_subtotal"])
        tax = _int(it["totals"]["line_total_tax"])
        total = _int(it["totals"]["line_total"]) + tax
        line_items.append({
            "id": str(it["id"]),
            "item": {"id": str(it["id"]), "quantity": qty},
            "base_amount": base, "discount": max(base - sub, 0), "subtotal": sub, "tax": tax, "total": total,
        })
        cart_items.append({"id": it["id"], "type": it["type"], "quantity": qty,
                           "parent_id": (it.get("item_data") or [{}])[0].get("parent_id")})
    options, selected = [], None
    shipping_cfg = m.get("shipping", {})
    now = datetime.now(timezone.utc)
    for pkg in cart.get("shipping_rates", []):
        for rt in pkg["shipping_rates"]:
            cfg = shipping_cfg.get(rt["rate_id"]) or shipping_cfg.get(rt.get("method_id", "")) or {}
            sub = _int(rt["price"]); tax = _int(rt.get("taxes") or 0)
            opt = {
                "type": "shipping", "id": rt["rate_id"],
                "title": cfg.get("title") or rt["name"],
                "subtitle": cfg.get("subtitle") or f"{cfg.get('min_days', 3)}–{cfg.get('max_days', 8)} business days",
                "carrier": cfg.get("carrier", "USPS"),
                "earliest_delivery_time": _rfc3339(now + timedelta(days=int(cfg.get("min_days", 3)))),
                "latest_delivery_time": _rfc3339(now + timedelta(days=int(cfg.get("max_days", 8)))),
                "subtotal": sub, "tax": tax, "total": sub + tax,
                "_method_id": rt.get("method_id", ""), "_package_id": pkg.get("package_id", 0),
            }
            options.append(opt)
            if rt.get("selected"):
                selected = opt
    if option_id:
        selected = next((o for o in options if o["id"] == option_id), selected)
    t = cart["totals"]
    items_base = sum(li["base_amount"] for li in line_items)
    items_disc = sum(li["discount"] for li in line_items)
    subtotal = _int(t["total_items"])
    fulfillment = _int(t["total_shipping"])
    tax = _int(t["total_tax"])
    total = _int(t["total_price"])
    totals = [
        {"type": "items_base_amount", "display_text": "Items", "amount": items_base},
        {"type": "items_discount", "display_text": "Item discounts", "amount": items_disc},
        {"type": "subtotal", "display_text": "Subtotal", "amount": subtotal},
        {"type": "discount", "display_text": "Discounts", "amount": _int(t.get("total_discount") or 0)},
        {"type": "fulfillment", "display_text": "Shipping", "amount": fulfillment},
        {"type": "tax", "display_text": "Tax", "amount": tax},
        {"type": "fee", "display_text": "Fees", "amount": _int(t.get("total_fees") or 0)},
        {"type": "total", "display_text": "Total", "amount": total},
    ]
    return {"currency": cur, "line_items": line_items, "fulfillment_options": options,
            "fulfillment_option_id": selected["id"] if selected else None, "totals": totals,
            "_cart_items": cart_items, "_selected": selected}

def _public_option(o):
    return {k: v for k, v in o.items() if not k.startswith("_")}

def session_view(m, s, quote, messages=None, order=None, include_provider=True):
    view = {
        "id": s["id"], "status": s["status"], "currency": quote["currency"],
        "line_items": quote["line_items"],
        "fulfillment_options": [_public_option(o) for o in quote["fulfillment_options"]],
        "fulfillment_option_id": quote["fulfillment_option_id"],
        "totals": quote["totals"], "messages": messages or [],
        "links": [{"type": "terms_of_use", "url": m["tos_url"]}, {"type": "privacy_policy", "url": m["privacy_url"]}]
                 + ([{"type": "seller_shop_policies", "url": m["policies_url"]}] if m.get("policies_url") else []),
    }
    if s.get("buyer"):
        view["buyer"] = s["buyer"]
    if s.get("address"):
        view["fulfillment_address"] = s["address"]
    if include_provider:
        view["payment_provider"] = {"provider": "stripe", "supported_payment_methods": ["card"]}
    if order:
        view["order"] = order
    return view

def _status_for(s, quote):
    if s["status"] in ("completed", "canceled", "in_progress"):
        return s["status"]
    return "ready_for_payment" if (s.get("address") and quote.get("fulfillment_option_id")) else "not_ready_for_payment"

# ----------------------------------------------------------------------------- session persistence
def _save_session(s):
    with _db_lock, _db() as c:
        c.execute("""INSERT OR REPLACE INTO sessions (id,merchant,status,cart_token,items,buyer,address,option_id,quote,quote_hash,order_id,created,updated)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (s["id"], s["merchant"], s["status"], s["cart_token"], _j(s["items"]), _j(s.get("buyer")),
                   _j(s.get("address")), s.get("option_id"), _j(s["quote"]), s.get("quote_hash"), s.get("order_id"),
                   s.get("created", _now()), _now()))

def _load_session(slug, sid):
    with _db() as c:
        r = c.execute("SELECT * FROM sessions WHERE id=? AND merchant=?", (sid, slug)).fetchone()
    if not r:
        raise ACPError(404, "invalid", "checkout session not found", param="$.checkout_session_id")
    return {"id": r["id"], "merchant": r["merchant"], "status": r["status"], "cart_token": r["cart_token"],
            "items": json.loads(r["items"]), "buyer": json.loads(r["buyer"]), "address": json.loads(r["address"]),
            "option_id": r["option_id"], "quote": json.loads(r["quote"]), "quote_hash": r["quote_hash"],
            "order_id": r["order_id"], "created": r["created"]}

def _requote(m, s):
    """Rebuild the Woo cart from the session's items/address/option and return a fresh quote."""
    w = Woo(m)
    tok = w.new_cart()
    cart = w.add_items(tok, s["items"])
    if s.get("address"):
        cart = w.set_address(tok, s["address"], s.get("buyer"))
        if s.get("option_id"):
            try:
                cart = w.select_rate(tok, s["option_id"])
            except ACPError:
                s["option_id"] = None
    s["cart_token"] = tok
    q = build_quote(m, cart, s.get("option_id"))
    s["quote"] = q
    s["quote_hash"] = hashlib.sha256(_j({"li": q["line_items"], "t": q["totals"], "o": q["fulfillment_option_id"]}).encode()).hexdigest()
    s["status"] = _status_for(s, q)
    return q

def _validate_items(items):
    if not isinstance(items, list) or not items:
        raise ACPError(400, "missing", "items[] is required", param="$.items")
    out = []
    for i, it in enumerate(items):
        if not isinstance(it, dict) or "id" not in it:
            raise ACPError(400, "missing", "items[].id is required", param=f"$.items[{i}].id")
        q = it.get("quantity", 1)
        if not isinstance(q, int) or q <= 0:
            raise ACPError(400, "invalid", "items[].quantity must be a positive integer", param=f"$.items[{i}].quantity")
        try:
            int(str(it["id"]))
        except ValueError:
            raise ACPError(400, "invalid", "items[].id must be the store's numeric product/variation id",
                           param=f"$.items[{i}].id")
        out.append({"id": str(it["id"]), "quantity": q})
    return out

# ----------------------------------------------------------------------------- endpoints
@bp.route("/acp/<slug>/checkout_sessions", methods=["POST"])
def create_session(slug):
    m = _auth(slug)
    ep = "create"
    replay, key, _ = _idem_start(slug, ep)
    if replay:
        return replay
    try:
        body = request.get_json(force=True, silent=True) or {}
        s = {"id": _sid(), "merchant": slug, "status": "not_ready_for_payment", "cart_token": None,
             "items": _validate_items(body.get("items")), "buyer": body.get("buyer"),
             "address": body.get("fulfillment_address"), "option_id": None, "created": _now()}
        q = _requote(m, s)
        _save_session(s)
        return _reply(slug, ep, key, 201, session_view(m, s, q))
    except ACPError:
        _idem_abort(slug, ep, key); raise
    except Exception as e:
        _idem_abort(slug, ep, key)
        raise ACPError(502, "invalid", f"store unavailable: {type(e).__name__}", etype="service_unavailable")

@bp.route("/acp/<slug>/checkout_sessions/<sid>", methods=["POST"])
def update_session(slug, sid):
    m = _auth(slug)
    ep = "update"
    replay, key, _ = _idem_start(slug, ep + ":" + sid)
    if replay:
        return replay
    try:
        s = _load_session(slug, sid)
        if s["status"] in ("completed", "canceled"):
            raise ACPError(405, "invalid", f"session is {s['status']}")
        body = request.get_json(force=True, silent=True) or {}
        if "items" in body:
            s["items"] = _validate_items(body["items"])
        if "buyer" in body:
            s["buyer"] = body["buyer"]
        if "fulfillment_address" in body:
            s["address"] = body["fulfillment_address"]
        if "fulfillment_option_id" in body:
            s["option_id"] = body["fulfillment_option_id"]
        q = _requote(m, s)
        msgs = []
        if body.get("fulfillment_option_id") and q["fulfillment_option_id"] != body["fulfillment_option_id"]:
            msgs.append({"type": "error", "code": "invalid", "param": "$.fulfillment_option_id",
                         "content_type": "plain", "content": "That shipping option is not available for this address."})
        _save_session(s)
        return _reply(slug, ep + ":" + sid, key, 200, session_view(m, s, q, msgs, include_provider=False))
    except ACPError:
        _idem_abort(slug, ep + ":" + sid, key); raise
    except Exception as e:
        _idem_abort(slug, ep + ":" + sid, key)
        raise ACPError(502, "invalid", f"store unavailable: {type(e).__name__}", etype="service_unavailable")

@bp.route("/acp/<slug>/checkout_sessions/<sid>", methods=["GET"])
def get_session(slug, sid):
    m = _auth(slug)
    s = _load_session(slug, sid)
    order = None
    if s.get("order_id"):
        order = {"id": s["order_id"], "checkout_session_id": s["id"], "permalink_url": _permalink(slug, s["order_id"])}
    return _echo_headers(Response(_j(session_view(m, s, s["quote"], order=order)), mimetype="application/json")), 200

@bp.route("/acp/<slug>/checkout_sessions/<sid>/cancel", methods=["POST"])
def cancel_session(slug, sid):
    m = _auth(slug)
    ep = "cancel:" + sid
    replay, key, _ = _idem_start(slug, ep)
    if replay:
        return replay
    s = _load_session(slug, sid)
    if s["status"] in ("completed", "in_progress"):
        _idem_abort(slug, ep, key)
        raise ACPError(405, "invalid", "completed sessions cannot be canceled")
    s["status"] = "canceled"
    _save_session(s)
    return _reply(slug, ep, key, 200, session_view(m, s, s["quote"], include_provider=False))

@bp.route("/acp/<slug>/checkout_sessions/<sid>/complete", methods=["POST"])
def complete_session(slug, sid):
    m = _auth(slug)
    ep = "complete:" + sid
    replay, key, _ = _idem_start(slug, ep)
    if replay:
        return replay
    try:
        s = _load_session(slug, sid)
        if s["status"] == "completed":
            order = {"id": s["order_id"], "checkout_session_id": s["id"], "permalink_url": _permalink(slug, s["order_id"])}
            return _reply(slug, ep, key, 200, session_view(m, s, s["quote"], order=order, include_provider=False))
        if s["status"] == "canceled":
            raise ACPError(405, "invalid", "session is canceled")
        body = request.get_json(force=True, silent=True) or {}
        pd = body.get("payment_data") or {}
        if body.get("buyer"):
            s["buyer"] = body["buyer"]
        if not pd.get("token"):
            raise ACPError(400, "missing", "payment_data.token is required", param="$.payment_data.token")
        if pd.get("provider", "stripe") != "stripe":
            raise ACPError(400, "invalid", "only provider=stripe is supported", param="$.payment_data.provider")
        # re-quote and refuse if anything drifted since the agent last saw the totals
        old_hash = s.get("quote_hash")
        q = _requote(m, s)
        if s["status"] != "ready_for_payment":
            _save_session(s)
            return _reply(slug, ep, key, 200, session_view(m, s, q, [{"type": "error", "code": "missing",
                          "param": "$.fulfillment_address", "content_type": "plain",
                          "content": "Shipping address and option are required before payment."}], include_provider=False))
        if old_hash and old_hash != s["quote_hash"]:
            _save_session(s)
            return _reply(slug, ep, key, 200, session_view(m, s, q, [{"type": "error", "code": "invalid", "param": "$.totals",
                          "content_type": "plain", "content": "Prices or availability changed; please review the updated totals."}],
                          include_provider=False))
        total = next(t["amount"] for t in q["totals"] if t["type"] == "total")
        # charge the shared payment token on the merchant's Stripe account
        pi = _stripe_charge(m, pd["token"], total, q["currency"], s["id"], (s.get("buyer") or {}).get("email"))
        if not pi["ok"]:
            _save_session(s)
            return _reply(slug, ep, key, 200, session_view(m, s, q, [{"type": "error", "code": "payment_declined",
                          "param": "$.payment_data.token", "content_type": "plain", "content": pi["message"]}], include_provider=False))
        pi_id = pi["id"]
        # create the Woo order (idempotent on session id); if the store is down, we're in_progress and a worker retries
        s["status"] = "in_progress"
        _save_session(s)
        order_id = None
        try:
            w = Woo(m)
            existing = w.find_order_by_session(s["id"])
            o = existing or w.create_order(s, q, s.get("buyer"), s.get("address"), pi_id, q["_selected"])
            order_id = str(o["id"])
        except Exception as e:
            import sys; print(f"[acp] order create failed for {s['id']}: {e}", file=sys.stderr)
            _queue(slug, "_retry_order", {"session_id": s["id"], "pi": pi_id})
        with _db_lock, _db() as c:
            c.execute("INSERT OR REPLACE INTO orders (id,merchant,session_id,woo_order_id,stripe_pi,amount,currency,status,email,created) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (order_id or s["id"], slug, s["id"], order_id, pi_id, total, q["currency"],
                       "created" if order_id else "pending_store", (s.get("buyer") or {}).get("email"), _now()))
        if order_id:
            s["status"] = "completed"; s["order_id"] = order_id
            _save_session(s)
            _queue(slug, "order_created", {"type": "order_created", "data": {"type": "order", "checkout_session_id": s["id"],
                   "permalink_url": _permalink(slug, order_id), "status": "confirmed", "refunds": []}})
        order = {"id": order_id or s["id"], "checkout_session_id": s["id"], "permalink_url": _permalink(slug, order_id or s["id"])}
        return _reply(slug, ep, key, 200, session_view(m, s, q, order=order, include_provider=False))
    except ACPError:
        _idem_abort(slug, ep, key); raise

# ----------------------------------------------------------------------------- Stripe
def _stripe_charge(m, spt, amount, currency, session_id, email=None):
    """One PaymentIntent from the Shared Payment Token. Returns {ok, id} or {ok: False, message}."""
    if MOCK_PAY:
        return {"ok": not spt.endswith("_declined"), "id": "pi_mock_" + secrets.token_hex(6), "message": "declined (mock)"}
    data = {
        "amount": amount, "currency": currency, "confirm": "true",
        "payment_method_data[type]": "card",
        "payment_method_data[shared_payment_granted_token]": spt,
        "automatic_payment_methods[enabled]": "true", "automatic_payment_methods[allow_redirects]": "never",
        "metadata[acp_checkout_session_id]": session_id, "metadata[gateway]": "canaishopyou",
        "description": f"{m.get('seller_name') or m['store_url']} — ChatGPT Instant Checkout",
    }
    if email:
        data["receipt_email"] = email
    headers = dict(UA)
    if m.get("stripe_version"):
        headers["Stripe-Version"] = m["stripe_version"]
    try:
        r = requests.post("https://api.stripe.com/v1/payment_intents", data=data, auth=(m["stripe_secret_key"], ""),
                          headers={**headers, "Idempotency-Key": f"acp-{session_id}"}, timeout=30)
        j = r.json()
    except Exception as e:
        return {"ok": False, "message": f"payment service unavailable ({type(e).__name__})"}
    if r.status_code == 200 and j.get("status") in ("succeeded", "processing", "requires_capture"):
        return {"ok": True, "id": j["id"]}
    err = j.get("error", {})
    return {"ok": False, "message": err.get("message") or f"payment declined ({j.get('status')})"}

def stripe_refund(m, pi_id, amount=None):
    data = {"payment_intent": pi_id}
    if amount:
        data["amount"] = amount
    r = requests.post("https://api.stripe.com/v1/refunds", data=data, auth=(m["stripe_secret_key"], ""), headers=UA, timeout=30)
    return r.json()

# ----------------------------------------------------------------------------- permalink (email-gated order page)
def _pl_secret():
    return os.environ.get("ACP_PERMALINK_SECRET") or "dev-permalink-secret"

def _permalink(slug, order_id):
    sig = hmac.new(_pl_secret().encode(), f"{slug}:{order_id}".encode(), hashlib.sha256).hexdigest()[:20]
    return f"{os.environ.get('ACP_PUBLIC_BASE', 'https://canaishopyou.com')}/acp/o/{slug}.{order_id}.{sig}"

PERMALINK_HTML = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Order {{oid}} · {{seller}}</title>
<style>body{font-family:-apple-system,system-ui,sans-serif;max-width:560px;margin:40px auto;padding:0 20px;color:#111}
.card{border:1px solid #e5e5e5;border-radius:12px;padding:20px;margin:16px 0}input{font-size:16px;padding:10px;width:100%;box-sizing:border-box}
button{font-size:16px;padding:10px 16px;margin-top:10px}.mut{color:#666}table{width:100%;border-collapse:collapse}td{padding:6px 0;border-bottom:1px solid #eee}</style>
<h2>{{seller}} — order #{{oid}}</h2>
{% if not order %}
<div class=card><p>Enter the email used at checkout to view this order.</p>
<form method=post><input name=email type=email placeholder="you@example.com" required autofocus><button>View order</button></form>
{% if bad %}<p class=mut>That email doesn't match this order.</p>{% endif %}</div>
{% else %}
<div class=card><p><b>Status:</b> {{order.status}}</p><p class=mut>Placed {{order.date_created}} · Paid via ChatGPT Instant Checkout</p>
<table>{% for li in order.line_items %}<tr><td>{{li.name}} × {{li.quantity}}</td><td style="text-align:right">{{cur}} {{li.total}}</td></tr>{% endfor %}
<tr><td>Shipping</td><td style="text-align:right">{{cur}} {{order.shipping_total}}</td></tr>
<tr><td><b>Total</b></td><td style="text-align:right"><b>{{cur}} {{order.total}}</b></td></tr></table>
<p class=mut>Ships to: {{order.shipping.first_name}} {{order.shipping.last_name}}, {{order.shipping.address_1}}, {{order.shipping.city}} {{order.shipping.state}} {{order.shipping.postcode}}</p>
<p class=mut>Questions: <a href="{{store}}/contact/">{{seller}} support</a></p></div>
{% endif %}"""

@bp.route("/acp/o/<token>", methods=["GET", "POST"])
def permalink(token):
    try:
        slug, oid, sig = token.split(".")
    except ValueError:
        return "Not found", 404
    want = hmac.new(_pl_secret().encode(), f"{slug}:{oid}".encode(), hashlib.sha256).hexdigest()[:20]
    m = _m(slug)
    if not m or not hmac.compare_digest(sig, want):
        return "Not found", 404
    order, bad = None, False
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        o = Woo(m).get_order(oid) if m.get("woo_ck") else None
        if o and (o.get("billing", {}).get("email", "").lower() == email):
            order = o
        else:
            bad = True
    return render_template_string(PERMALINK_HTML, oid=oid, seller=m.get("seller_name") or m["store_url"], order=order, bad=bad,
                                   cur=(m.get("currency") or "USD").upper(), store=m["store_url"].rstrip("/"))

# ----------------------------------------------------------------------------- outbound webhooks (outbox) + retries
def _queue(slug, event, payload):
    with _db_lock, _db() as c:
        c.execute("INSERT INTO outbox (merchant,event,payload,attempts,next_at) VALUES (?,?,?,0,?)", (slug, event, _j(payload), _now()))
    _kick()

def _deliver(row):
    m = _m(row["merchant"])
    payload = json.loads(row["payload"])
    if row["event"] == "_retry_order":       # internal: finish an order whose store call failed at /complete
        s = _load_session(row["merchant"], payload["session_id"])
        w = Woo(m)
        o = w.find_order_by_session(s["id"]) or w.create_order(s, s["quote"], s.get("buyer"), s.get("address"), payload["pi"], s["quote"].get("_selected"))
        s["status"], s["order_id"] = "completed", str(o["id"])
        _save_session(s)
        with _db_lock, _db() as c:
            c.execute("UPDATE orders SET woo_order_id=?, status='created', id=? WHERE session_id=?", (str(o["id"]), str(o["id"]), s["id"]))
        _queue(row["merchant"], "order_updated", {"type": "order_updated", "data": {"type": "order", "checkout_session_id": s["id"],
               "permalink_url": _permalink(row["merchant"], str(o["id"])), "status": "confirmed", "refunds": []}})
        return True
    url, key = m.get("openai_webhook_url"), m.get("openai_webhook_key")
    if not url:
        return True   # nothing registered yet (OpenAI provisions this at merchant approval) — mark delivered
    body = _j(payload).encode()
    sig = hmac.new((key or "").encode(), body, hashlib.sha256).hexdigest()
    r = requests.post(url, data=body, headers={**UA, "Content-Type": "application/json", "Merchant-Signature": sig,
                                               "Merchant_Name-Signature": sig, "Request-Id": str(uuid.uuid4())}, timeout=15)
    return 200 <= r.status_code < 300

_worker_started = False
_worker_lock = threading.Lock()

def _kick():
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
    threading.Thread(target=_worker, daemon=True).start()

def _worker():
    while True:
        try:
            with _db() as c:
                rows = c.execute("SELECT * FROM outbox WHERE delivered IS NULL AND next_at <= ? AND attempts < 12 ORDER BY id LIMIT 10", (_now(),)).fetchall()
            for row in rows:
                ok = False
                try:
                    ok = _deliver(row)
                except Exception as e:
                    import sys; print(f"[acp] outbox {row['id']} {row['event']} failed: {e}", file=sys.stderr)
                with _db_lock, _db() as c:
                    if ok:
                        c.execute("UPDATE outbox SET delivered=? WHERE id=?", (_now(), row["id"]))
                    else:
                        c.execute("UPDATE outbox SET attempts=attempts+1, next_at=? WHERE id=?",
                                  (_now() + min(3600, 30 * (2 ** row["attempts"])), row["id"]))
        except Exception as e:
            import sys; print(f"[acp] worker error: {e}", file=sys.stderr)
        time.sleep(5)

# ----------------------------------------------------------------------------- inbound: WooCommerce order.updated
WOO_STATUS = {"processing": "confirmed", "on-hold": "manual_review", "completed": "fulfilled", "cancelled": "canceled",
              "refunded": "canceled", "failed": "canceled", "shipped": "shipped"}

@bp.route("/acp/<slug>/webhooks/woo", methods=["POST"])
def woo_webhook(slug):
    m = _m(slug)
    if not m:
        return "", 404
    raw = request.get_data()
    secret = m.get("woo_webhook_secret")
    if secret:
        want = base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()
        if not hmac.compare_digest(request.headers.get("X-WC-Webhook-Signature", ""), want):
            return "bad signature", 401
    o = request.get_json(silent=True) or {}
    if not o.get("id"):
        return "", 200   # Woo sends a ping on webhook creation
    ext = f"{o['id']}:{o.get('date_modified')}"
    with _db_lock, _db() as c:
        if c.execute("SELECT 1 FROM inbound WHERE source='woo' AND ext_id=?", (ext,)).fetchone():
            return "", 200
        c.execute("INSERT INTO inbound VALUES ('woo',?,?)", (ext, _now()))
        row = c.execute("SELECT * FROM orders WHERE merchant=? AND woo_order_id=?", (slug, str(o["id"]))).fetchone()
    if not row:
        return "", 200   # not one of ours
    status = WOO_STATUS.get(o.get("status"), "confirmed")
    refunds = []
    refunded_total = sum(abs(int(round(float(r.get("total", 0)) * 100))) for r in o.get("refunds", []))
    if refunded_total and refunded_total > (row["refunded"] or 0):
        # merchant refunded in Woo -> mirror on Stripe (Woo can't refund a PaymentIntent it didn't create)
        delta = refunded_total - (row["refunded"] or 0)
        if row["stripe_pi"] and not row["stripe_pi"].startswith("pi_mock_"):
            stripe_refund(m, row["stripe_pi"], delta)
        with _db_lock, _db() as c:
            c.execute("UPDATE orders SET refunded=? WHERE id=?", (refunded_total, row["id"]))
        refunds = [{"type": "original_payment", "amount": refunded_total}]
    with _db_lock, _db() as c:
        c.execute("UPDATE orders SET status=? WHERE id=?", (status, row["id"]))
    _queue(slug, "order_updated", {"type": "order_updated", "data": {"type": "order", "checkout_session_id": row["session_id"],
           "permalink_url": _permalink(slug, row["woo_order_id"]), "status": status, "refunds": refunds}})
    return "", 200

# ----------------------------------------------------------------------------- health
@bp.route("/acp/<slug>/health")
def health(slug):
    m = _m(slug)
    if not m:
        return jsonify({"ok": False, "error": "unknown merchant"}), 404
    out = {"ok": True, "merchant": slug, "api_version": API_VERSION, "mock_payments": MOCK_PAY}
    try:
        w = Woo(m); tok = w.new_cart(); out["store_api"] = "ok"
    except Exception as e:
        out["ok"] = False; out["store_api"] = f"fail: {type(e).__name__}"
    out["woo_keys"] = bool(m.get("woo_ck") and m.get("woo_cs"))
    out["stripe_key"] = bool(m.get("stripe_secret_key"))
    out["openai_webhook"] = bool(m.get("openai_webhook_url"))
    return jsonify(out), (200 if out["ok"] else 503)
