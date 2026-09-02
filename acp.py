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
import base64, hashlib, hmac, ipaddress, json, os, re, secrets, sqlite3, threading, time, uuid
from collections import deque
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
ENFORCE_IP = os.environ.get("ACP_ENFORCE_IP") == "1"    # only accept session calls from OpenAI's published egress ranges
RATE_LIMIT = int(os.environ.get("ACP_RATE_LIMIT", "120"))  # requests / minute / (merchant, ip)
HERE = os.path.dirname(os.path.abspath(__file__))

# ----------------------------------------------------------------------------- merchants
_MERCHANTS = None

def merchants():
    """{slug: config}. Sources, merged in this order: env ACP_MERCHANTS (JSON), acp_merchants.json next to this file
    (gitignored), and merchants onboarded at runtime through /acp/onboard (kept in the app data file).
    Required per merchant: store_url, bearer_key, tos_url, privacy_url. For /complete: woo_ck, woo_cs, stripe_secret_key.
    Optional: policies_url, currency (default from cart), seller_name, openai_webhook_url, openai_webhook_key,
              acp_signature_key, woo_webhook_secret, stripe_webhook_secret,
              shipping: {"<woo rate_id or method_id>": {carrier, min_days, max_days, title, subtitle}}"""
    global _MERCHANTS
    if _MERCHANTS is None:
        cfg = {}
        p = os.path.join(HERE, "acp_merchants.json")
        if os.path.exists(p):
            try: cfg.update(json.load(open(p)))
            except Exception: pass
        if os.environ.get("ACP_MERCHANTS"):
            cfg.update(json.loads(os.environ["ACP_MERCHANTS"]))
        _MERCHANTS = cfg
    out = dict(_MERCHANTS)
    out.update(_runtime_merchants())
    return out

def _runtime_merchants():
    try:
        return json.load(open(os.environ.get("DATA_FILE", "/tmp/cani_data.json"))).get("acp_merchants", {})
    except Exception:
        return {}

def _m(slug):
    return merchants().get(slug)

# ----------------------------------------------------------------------------- OpenAI egress allowlist + rate limit
_ip_cache = {"nets": [], "at": 0, "refreshing": False}
_ip_lock = threading.Lock()

def _refresh_openai_nets():
    try:
        d = requests.get("https://openai.com/chatgpt-connectors.json", headers=UA, timeout=10).json()
        nets = []
        for p in d.get("prefixes", []):
            for k in ("ipv4Prefix", "ipv6Prefix"):
                if p.get(k):
                    nets.append(ipaddress.ip_network(p[k], strict=False))
        if nets:
            _ip_cache.update(nets=nets, at=_now())
        else:
            _ip_cache["at"] = _now() - 3000
    except Exception:
        _ip_cache["at"] = _now() - 3000  # retry in 10 min, keep old list
    finally:
        _ip_cache["refreshing"] = False

def _openai_nets(block=False):
    """Cached OpenAI egress prefixes; refreshed hourly off the request path (block=True only for tests/startup)."""
    if _now() - _ip_cache["at"] > 3600:
        with _ip_lock:
            if not _ip_cache["refreshing"]:
                _ip_cache["refreshing"] = True
                if block:
                    _refresh_openai_nets()
                else:
                    threading.Thread(target=_refresh_openai_nets, daemon=True).start()
    return _ip_cache["nets"]

if ENFORCE_IP:
    threading.Thread(target=_refresh_openai_nets, daemon=True).start()

def _client_ip():
    return (request.headers.get("X-Forwarded-For", request.remote_addr or "")).split(",")[0].strip()

def _ip_allowed(ip):
    nets = _openai_nets()
    if not nets:
        return True  # list unavailable: don't lock ourselves out
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(a in n for n in nets)

_buckets = {}
_buckets_lock = threading.Lock()

def _rate_ok(key):
    now = _now()
    with _buckets_lock:
        q = _buckets.setdefault(key, deque())
        while q and q[0] < now - 60:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            return False
        q.append(now)
        if len(_buckets) > 5000:  # crude GC
            for k in [k for k, v in _buckets.items() if not v or v[-1] < now - 120][:1000]:
                _buckets.pop(k, None)
    return True

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
        try:
            c.execute("ALTER TABLE sessions ADD COLUMN drift INTEGER DEFAULT 0")  # totals changed; needs an update before complete
        except sqlite3.OperationalError:
            pass

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
        # same answer as a bad bearer, so merchant slugs can't be enumerated
        raise ACPError(401, "unauthorized", "invalid bearer token")
    ip = _client_ip()
    if ENFORCE_IP and not _ip_allowed(ip):
        raise ACPError(403, "forbidden", "source address not in the ChatGPT egress allowlist")
    if not _rate_ok(f"{slug}:{ip}"):
        raise ACPError(429, "rate_limited", "too many requests; retry shortly", etype="processing_error")
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
                j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                wc, msg = (j.get("code") or ""), re.sub(r"&quot;", '"', (j.get("message") or r.text or "")[:200])
                param = f"$.items[?(@.id=='{it['id']}')]"
                if "stock" in msg.lower() or "out_of_stock" in wc:
                    raise ACPError(400, "out_of_stock", msg or "item is out of stock", param=param)
                if wc == "woocommerce_rest_missing_attributes":
                    raise ACPError(400, "invalid", f"item {it['id']} is a variable product; use the id of a specific variant "
                                   "(each variant is its own row in the product feed)", param=param)
                if wc == "woocommerce_rest_cart_invalid_product":
                    raise ACPError(400, "invalid", f"item {it['id']} does not exist in this store", param=param)
                raise ACPError(400, "invalid", msg or "item rejected by store", param=param)
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
            # Woo REST resolves the parent product from variation_id itself; passing product_id=variation_id is wrong
            li = {"quantity": it["quantity"], ("variation_id" if it["type"] == "variation" else "product_id"): it["id"]}
            line_items.append(li)
        body = {
            "status": "processing", "set_paid": True, "payment_method": "stripe",
            "payment_method_title": "ChatGPT Instant Checkout (Stripe)", "transaction_id": pi_id,
            "currency": quote["currency"].upper(),
            "billing": dict(a, email=(buyer or {}).get("email", ""), phone=(buyer or {}).get("phone_number", "") or ""),
            "shipping": a, "line_items": line_items,
            "shipping_lines": [{"method_id": option["_method_id"] or "flat_rate", "method_title": option["title"],
                                "total": f"{option['total'] / 100:.2f}"}] if option and option.get("type") == "shipping" else [],
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
        cart_items.append({"id": it["id"], "type": it["type"], "quantity": qty})
    options, selected = [], None
    shipping_cfg = m.get("shipping", {})
    now = datetime.now(timezone.utc)
    packages = cart.get("shipping_rates", [])
    if not cart.get("needs_shipping", True):
        # digital-only cart: no address needed, one zero-cost digital option (spec: type=digital, no carrier/times)
        options = [{"type": "digital", "id": "digital", "title": "Digital delivery", "subtitle": "Delivered by email",
                    "subtotal": 0, "tax": 0, "total": 0, "_method_id": "", "_package_id": 0}]
        selected = options[0]
    elif len(packages) <= 1:
        for pkg in packages:
            for rt in pkg["shipping_rates"]:
                opt = _ship_option(m, shipping_cfg, rt, now, pkg.get("package_id", 0))
                options.append(opt)
                if rt.get("selected"):
                    selected = opt
    else:
        # multi-package cart (e.g. dropship + warehouse): one option per rate *name* that exists in every package, summed
        by_name = {}
        for pkg in packages:
            for rt in pkg["shipping_rates"]:
                by_name.setdefault(rt["name"], []).append((pkg.get("package_id", 0), rt))
        for name, rts in by_name.items():
            if len(rts) != len(packages):
                continue
            base = _ship_option(m, shipping_cfg, rts[0][1], now, rts[0][0])
            base["id"] = "multi:" + hashlib.sha1(name.encode()).hexdigest()[:10]
            base["subtotal"] = sum(_int(r["price"]) for _, r in rts)
            base["tax"] = sum(_int(r.get("taxes") or 0) for _, r in rts)
            base["total"] = base["subtotal"] + base["tax"]
            base["_rates"] = [(pid, r["rate_id"]) for pid, r in rts]
            options.append(base)
            if all(r.get("selected") for _, r in rts):
                selected = base
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

def _ship_option(m, shipping_cfg, rt, now, package_id):
    cfg = shipping_cfg.get(rt["rate_id"]) or shipping_cfg.get(rt.get("method_id", "")) or _guess_shipping(rt["name"])
    sub = _int(rt["price"]); tax = _int(rt.get("taxes") or 0)
    return {
        "type": "shipping", "id": rt["rate_id"],
        "title": cfg.get("title") or rt["name"],
        "subtitle": cfg.get("subtitle") or f"{cfg.get('min_days', 3)}–{cfg.get('max_days', 8)} business days",
        "carrier": cfg.get("carrier", "USPS"),
        "earliest_delivery_time": _rfc3339(now + timedelta(days=int(cfg.get("min_days", 3)))),
        "latest_delivery_time": _rfc3339(now + timedelta(days=int(cfg.get("max_days", 8)))),
        "subtotal": sub, "tax": tax, "total": sub + tax,
        "_method_id": rt.get("method_id", ""), "_package_id": package_id,
    }

def _guess_shipping(name):
    """Fallback carrier/window from the rate name when the merchant hasn't configured it (onboarding fills this properly)."""
    n = (name or "").lower()
    for kw, cfg in (("overnight", {"carrier": "UPS", "min_days": 1, "max_days": 2}),
                    ("next day", {"carrier": "UPS", "min_days": 1, "max_days": 2}),
                    ("express", {"carrier": "UPS", "min_days": 1, "max_days": 3}),
                    ("expedited", {"carrier": "UPS", "min_days": 2, "max_days": 4}),
                    ("priority", {"carrier": "USPS", "min_days": 2, "max_days": 4}),
                    ("2-day", {"carrier": "UPS", "min_days": 2, "max_days": 3}),
                    ("fedex", {"carrier": "FedEx", "min_days": 2, "max_days": 6}),
                    ("ups", {"carrier": "UPS", "min_days": 2, "max_days": 6}),
                    ("dhl", {"carrier": "DHL", "min_days": 3, "max_days": 8}),
                    ("pickup", {"carrier": "Local pickup", "min_days": 0, "max_days": 2})):
        if kw in n:
            return cfg
    return {"carrier": "USPS", "min_days": 4, "max_days": 9}

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
    if s.get("drift"):
        return "not_ready_for_payment"   # buyer hasn't seen the changed totals yet (cleared by the next update)
    sel = quote.get("_selected") or {}
    if sel.get("type") == "digital":
        return "ready_for_payment" if (s.get("buyer") or {}).get("email") else "not_ready_for_payment"
    return "ready_for_payment" if (s.get("address") and quote.get("fulfillment_option_id")) else "not_ready_for_payment"

# ----------------------------------------------------------------------------- session persistence
def _save_session(s):
    with _db_lock, _db() as c:
        c.execute("""INSERT OR REPLACE INTO sessions (id,merchant,status,cart_token,items,buyer,address,option_id,quote,quote_hash,order_id,created,updated,drift)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (s["id"], s["merchant"], s["status"], s["cart_token"], _j(s["items"]), _j(s.get("buyer")),
                   _j(s.get("address")), s.get("option_id"), _j(s["quote"]), s.get("quote_hash"), s.get("order_id"),
                   s.get("created", _now()), _now(), 1 if s.get("drift") else 0))

def _load_session(slug, sid):
    with _db() as c:
        r = c.execute("SELECT * FROM sessions WHERE id=? AND merchant=?", (sid, slug)).fetchone()
    if not r:
        raise ACPError(404, "invalid", "checkout session not found", param="$.checkout_session_id")
    return {"id": r["id"], "merchant": r["merchant"], "status": r["status"], "cart_token": r["cart_token"],
            "items": json.loads(r["items"]), "buyer": json.loads(r["buyer"]), "address": json.loads(r["address"]),
            "option_id": r["option_id"], "quote": json.loads(r["quote"]), "quote_hash": r["quote_hash"],
            "order_id": r["order_id"], "created": r["created"], "drift": bool(r["drift"]) if "drift" in r.keys() else False}

def _requote(m, s):
    """Rebuild the Woo cart from the session's items/address/option and return a fresh quote."""
    w = Woo(m)
    tok = w.new_cart()
    cart = w.add_items(tok, s["items"])
    if s.get("address"):
        cart = w.set_address(tok, s["address"], s.get("buyer"))
        if s.get("option_id") and s["option_id"] != "digital":
            try:
                if s["option_id"].startswith("multi:"):
                    pre = build_quote(m, cart)
                    opt = next((o for o in pre["fulfillment_options"] if o["id"] == s["option_id"]), None)
                    if not opt:
                        raise ACPError(400, "invalid", "shipping option unavailable", param="$.fulfillment_option_id")
                    for pid, rid in opt["_rates"]:
                        cart = w.select_rate(tok, rid, pid)
                else:
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
        if isinstance(q, bool) or not isinstance(q, int) or q <= 0:
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
        if s["status"] in ("completed", "canceled", "in_progress"):
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
        s["drift"] = False   # the agent is fetching fresh totals, so they'll be shown before any complete
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
        if s["status"] == "in_progress":
            # payment already taken, store order still being created by the worker — never charge twice
            order = {"id": s["id"], "checkout_session_id": s["id"], "permalink_url": _permalink(slug, s["id"])}
            return _reply(slug, ep, key, 200, session_view(m, s, s["quote"], [{"type": "info", "code": "in_progress",
                          "content_type": "plain", "content": "Payment received; the store is confirming your order."}],
                          order=order, include_provider=False))
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
        if s.get("drift"):
            _save_session(s)
            return _reply(slug, ep, key, 200, session_view(m, s, q, [{"type": "error", "code": "invalid", "param": "$.totals",
                          "content_type": "plain", "content": "Totals changed; refresh the checkout (update) before paying."}],
                          include_provider=False))
        if s["status"] != "ready_for_payment":
            _save_session(s)
            return _reply(slug, ep, key, 200, session_view(m, s, q, [{"type": "error", "code": "missing",
                          "param": "$.fulfillment_address", "content_type": "plain",
                          "content": "Shipping address and option are required before payment."}], include_provider=False))
        if old_hash and old_hash != s["quote_hash"]:
            # totals drifted since the agent last showed them: refuse, and require an update call (which recomputes
            # readiness) before any further complete — the buyer must see the new number first
            s["drift"] = True
            s["status"] = "not_ready_for_payment"
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
    except Exception as e:
        # transport/store failure BEFORE payment: release the idempotency key so a retry can succeed
        import sys; print(f"[acp] complete {sid} failed pre-payment: {e}", file=sys.stderr)
        _idem_abort(slug, ep, key)
        raise ACPError(502, "invalid", f"store unavailable: {type(e).__name__}", etype="service_unavailable")

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
        with _db() as c:
            orow = c.execute("SELECT status FROM orders WHERE session_id=?", (s["id"],)).fetchone()
        if orow and orow["status"] == "canceled":
            return True   # refunded meanwhile (reconcile ?fix=1) — never create the store order after a refund
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

def _give_up_order(row):
    """All retries to create the store order failed: refund the PaymentIntent, cancel the session, notify OpenAI."""
    m = _m(row["merchant"]); payload = json.loads(row["payload"])
    with _db() as c:
        o = c.execute("SELECT * FROM orders WHERE session_id=?", (payload["session_id"],)).fetchone()
    if not o or o["status"] == "canceled":
        return
    res = {"id": "re_mock"} if (o["stripe_pi"] or "").startswith("pi_mock_") else stripe_refund(m, o["stripe_pi"])
    import sys; print(f"[acp] gave up on order for {payload['session_id']}: refund {res.get('id')} {res.get('error')}", file=sys.stderr)
    with _db_lock, _db() as c:
        c.execute("UPDATE orders SET status='canceled', refunded=amount WHERE id=?", (o["id"],))
        c.execute("UPDATE sessions SET status='canceled' WHERE id=?", (payload["session_id"],))
    _queue(row["merchant"], "order_updated", {"type": "order_updated", "data": {"type": "order", "checkout_session_id": payload["session_id"],
           "permalink_url": _permalink(row["merchant"], o["id"]), "status": "canceled",
           "refunds": [{"type": "original_payment", "amount": o["amount"]}]}})

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
                if not ok and row["event"] == "_retry_order" and row["attempts"] + 1 >= 12:
                    _give_up_order(row)   # store never accepted the order: refund the buyer, tell OpenAI
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

# ----------------------------------------------------------------------------- inbound: Stripe (refunds / disputes issued outside Woo)
def _stripe_sig_ok(secret, raw, header):
    """Stripe-Signature: t=<ts>,v1=<hex hmac sha256 of "<ts>.<raw>">"""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts, v1 = parts["t"], parts["v1"]
    except Exception:
        return False
    if abs(_now() - int(ts)) > TS_WINDOW:
        return False
    want = hmac.new(secret.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, v1)

@bp.route("/acp/<slug>/webhooks/stripe", methods=["POST"])
def stripe_webhook(slug):
    m = _m(slug)
    if not m:
        return "", 404
    raw = request.get_data()
    secret = m.get("stripe_webhook_secret")
    if secret and not _stripe_sig_ok(secret, raw, request.headers.get("Stripe-Signature", "")):
        return "bad signature", 401
    ev = request.get_json(silent=True) or {}
    if not ev.get("id"):
        return "", 200
    with _db_lock, _db() as c:
        if c.execute("SELECT 1 FROM inbound WHERE source='stripe' AND ext_id=?", (ev["id"],)).fetchone():
            return "", 200
        c.execute("INSERT INTO inbound VALUES ('stripe',?,?)", (ev["id"], _now()))
    obj = (ev.get("data") or {}).get("object") or {}
    pi = obj.get("payment_intent") if isinstance(obj.get("payment_intent"), str) else (obj.get("payment_intent") or {}).get("id")
    if ev.get("type", "").startswith("payment_intent."):
        pi = obj.get("id")
    if not pi:
        return "", 200
    with _db() as c:
        row = c.execute("SELECT * FROM orders WHERE merchant=? AND stripe_pi=?", (slug, pi)).fetchone()
    if not row:
        return "", 200
    t = ev.get("type")
    if t == "charge.refunded":
        refunded = int(obj.get("amount_refunded") or 0)
        if refunded > (row["refunded"] or 0) and row["woo_order_id"] and m.get("woo_ck"):
            # refund happened in Stripe (dashboard/dispute) -> record it in Woo without re-charging the PSP
            delta = refunded - (row["refunded"] or 0)
            try:
                requests.post(f"{m['store_url'].rstrip('/')}/wp-json/wc/v3/orders/{row['woo_order_id']}/refunds",
                              json={"amount": f"{delta / 100:.2f}", "reason": "Refunded via Stripe", "api_refund": False},
                              auth=(m["woo_ck"], m["woo_cs"]), headers=UA, timeout=30)
            except Exception:
                pass
        status = "canceled" if refunded >= (row["amount"] or 0) else row["status"]
        with _db_lock, _db() as c:
            c.execute("UPDATE orders SET refunded=?, status=? WHERE id=?", (refunded, status, row["id"]))
        _queue(slug, "order_updated", {"type": "order_updated", "data": {"type": "order", "checkout_session_id": row["session_id"],
               "permalink_url": _permalink(slug, row["woo_order_id"] or row["id"]), "status": status,
               "refunds": [{"type": "original_payment", "amount": refunded}]}})
    elif t == "charge.dispute.created":
        with _db_lock, _db() as c:
            c.execute("UPDATE orders SET status='manual_review' WHERE id=?", (row["id"],))
        if row["woo_order_id"] and m.get("woo_ck"):
            try:
                requests.put(f"{m['store_url'].rstrip('/')}/wp-json/wc/v3/orders/{row['woo_order_id']}",
                             json={"status": "on-hold", "customer_note": "Payment disputed on Stripe — on hold"},
                             auth=(m["woo_ck"], m["woo_cs"]), headers=UA, timeout=30)
            except Exception:
                pass
        _queue(slug, "order_updated", {"type": "order_updated", "data": {"type": "order", "checkout_session_id": row["session_id"],
               "permalink_url": _permalink(slug, row["woo_order_id"] or row["id"]), "status": "manual_review", "refunds": []}})
    return "", 200

# ----------------------------------------------------------------------------- reconciliation (admin)
def _admin_ok():
    k = os.environ.get("ACP_ADMIN_KEY")
    given = request.headers.get("X-Admin-Key") or request.args.get("key", "")
    return bool(k) and hmac.compare_digest(given, k)

def _stripe_pi_status(m, pi_id):
    if pi_id.startswith("pi_mock_") or not m.get("stripe_secret_key"):
        return "mock"
    try:
        r = requests.get(f"https://api.stripe.com/v1/payment_intents/{pi_id}", auth=(m["stripe_secret_key"], ""), headers=UA, timeout=20)
        return r.json().get("status", f"http_{r.status_code}")
    except Exception as e:
        return f"error:{type(e).__name__}"

@bp.route("/acp/<slug>/reconcile")
def reconcile(slug):
    """Every order must have a succeeded PaymentIntent AND an existing Woo order. ?fix=1 refunds orphans
    (paid > 2h ago, no Woo order, retries exhausted)."""
    if not _admin_ok():
        return jsonify({"error": "admin key required"}), 401
    m = _m(slug)
    if not m:
        return jsonify({"error": "unknown merchant"}), 404
    fix = request.args.get("fix") == "1"
    w = Woo(m)
    report = {"merchant": slug, "checked": 0, "ok": 0, "issues": [], "fixed": []}
    with _db() as c:
        rows = c.execute("SELECT * FROM orders WHERE merchant=? ORDER BY created DESC LIMIT 500", (slug,)).fetchall()
    for r in rows:
        report["checked"] += 1
        pi_status = _stripe_pi_status(m, r["stripe_pi"] or "")
        woo = w.get_order(r["woo_order_id"]) if (r["woo_order_id"] and m.get("woo_ck")) else None
        issue = None
        if pi_status not in ("succeeded", "mock", "processing"):
            issue = f"payment {pi_status}"
        elif r["woo_order_id"] and not woo:
            issue = "woo order missing"
        elif not r["woo_order_id"]:
            issue = "no woo order (pending_store)"
            if fix and _now() - r["created"] > 7200:
                res = stripe_refund(m, r["stripe_pi"]) if pi_status != "mock" else {"id": "re_mock"}
                with _db_lock, _db() as c:
                    c.execute("UPDATE orders SET status='canceled', refunded=amount WHERE id=?", (r["id"],))
                report["fixed"].append({"order": r["id"], "refund": res.get("id"), "error": (res.get("error") or {}).get("message")})
        elif woo and woo.get("status") == "trash":
            issue = "woo order trashed"
        if issue:
            report["issues"].append({"order": r["id"], "session": r["session_id"], "pi": r["stripe_pi"], "issue": issue,
                                     "amount": r["amount"], "created": _rfc3339(datetime.fromtimestamp(r["created"], timezone.utc))})
        else:
            report["ok"] += 1
    with _db() as c:
        report["outbox_failing"] = [dict(x) for x in c.execute(
            "SELECT id,event,attempts,next_at FROM outbox WHERE merchant=? AND delivered IS NULL AND attempts>=3", (slug,)).fetchall()]
    return jsonify(report)

@bp.route("/acp/<slug>/status")
def merchant_status(slug):
    """Merchant-facing summary (bearer key)."""
    m = _auth(slug)
    with _db() as c:
        n_sess = c.execute("SELECT COUNT(*) FROM sessions WHERE merchant=?", (slug,)).fetchone()[0]
        by = dict(c.execute("SELECT status, COUNT(*) FROM sessions WHERE merchant=? GROUP BY status", (slug,)).fetchall())
        orders = [dict(x) for x in c.execute("SELECT id, woo_order_id, amount, currency, status, created FROM orders WHERE merchant=? ORDER BY created DESC LIMIT 20", (slug,)).fetchall()]
        revenue = c.execute("SELECT COALESCE(SUM(amount - refunded),0) FROM orders WHERE merchant=? AND status NOT IN ('canceled')", (slug,)).fetchone()[0]
        undelivered = c.execute("SELECT COUNT(*) FROM outbox WHERE merchant=? AND delivered IS NULL", (slug,)).fetchone()[0]
    return jsonify({"merchant": slug, "sessions": n_sess, "sessions_by_status": by, "orders": orders,
                    "net_revenue_minor": revenue, "webhooks_pending": undelivered,
                    "endpoints": {"base": f"{os.environ.get('ACP_PUBLIC_BASE', 'https://canaishopyou.com')}/acp/{slug}",
                                  "woo_webhook": f"{os.environ.get('ACP_PUBLIC_BASE', 'https://canaishopyou.com')}/acp/{slug}/webhooks/woo",
                                  "stripe_webhook": f"{os.environ.get('ACP_PUBLIC_BASE', 'https://canaishopyou.com')}/acp/{slug}/webhooks/stripe"}})

# ----------------------------------------------------------------------------- onboarding (store URL + keys -> configured merchant)
def _ai_key():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    for p in (os.path.join(HERE, "../cani/.anthropic_key"), os.path.join(HERE, ".anthropic_key")):
        if os.path.exists(p):
            return open(p).read().strip()
    return None

def _llm_shipping_map(store_url, rates, policy_text):
    """Ask Claude to map each Woo shipping rate to carrier + delivery window using the store's own shipping policy.
    Falls back to _guess_shipping. Returns {rate_id: {carrier, min_days, max_days, title, subtitle}}."""
    fallback = {r["rate_id"]: dict(_guess_shipping(r["name"]), title=r["name"]) for r in rates}
    key = _ai_key()
    if not key or not rates:
        return fallback
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key, timeout=30.0, max_retries=1)
        tool = {"name": "shipping_map", "description": "Carrier and delivery window per shipping rate",
                "input_schema": {"type": "object", "properties": {"rates": {"type": "array", "items": {"type": "object", "properties": {
                    "rate_id": {"type": "string"}, "carrier": {"type": "string", "description": "USPS, UPS, FedEx, DHL, or the carrier named in the policy"},
                    "min_days": {"type": "integer", "description": "earliest delivery, business days from order incl. handling"},
                    "max_days": {"type": "integer"}, "title": {"type": "string"}, "subtitle": {"type": "string", "description": "short, e.g. '5–8 business days'"}},
                    "required": ["rate_id", "carrier", "min_days", "max_days", "title", "subtitle"]}}}, "required": ["rates"]}}
        msg = client.messages.create(
            model=os.environ.get("ACP_AI_MODEL", "claude-sonnet-5"), max_tokens=800,
            system="You configure checkout shipping options for an online store. Use only the store's shipping policy text and the rate names. "
                   "If the policy gives handling/production time, add it to the window. Be conservative on max_days. Never invent a carrier the policy contradicts.",
            tools=[tool], tool_choice={"type": "tool", "name": "shipping_map"},
            messages=[{"role": "user", "content": f"Store: {store_url}\n\nShipping rates (Woo): {json.dumps([{k: r[k] for k in ('rate_id','name','price')} for r in rates])}\n\nShipping policy text:\n{policy_text[:6000] or '(none found)'}"}])
        out = next(b.input for b in msg.content if getattr(b, "type", "") == "tool_use")
        res = dict(fallback)
        for r in out.get("rates", []):
            if r.get("rate_id") in res:
                res[r["rate_id"]] = {k: r[k] for k in ("carrier", "min_days", "max_days", "title", "subtitle") if k in r}
        return res
    except Exception as e:
        import sys; print(f"[acp] llm shipping map failed: {e}", file=sys.stderr)
        return fallback

def _store_policy_text(store_url, keywords=("shipping", "delivery")):
    try:
        import feed_engine
        pages = requests.get(f"{store_url.rstrip('/')}/wp-json/wp/v2/pages?per_page=100&_fields=slug,link,content", headers=UA, timeout=20).json()
        for pg in pages if isinstance(pages, list) else []:
            if any(k in (pg.get("slug") or "") for k in keywords):
                return feed_engine._txt((pg.get("content") or {}).get("rendered", ""), cap=8000)
    except Exception:
        pass
    return ""

def onboard_merchant(store_url, woo_ck=None, woo_cs=None, stripe_secret_key=None, seller_name=None, contact_email=None):
    """Run every connectivity check, discover policies + shipping, build the merchant config. Returns (config|None, checks)."""
    import feed_engine
    store_url = "https://" + re.sub(r"^https?://", "", store_url.strip().lower()).split("/")[0]
    domain = store_url[8:]
    checks, cfg = {}, {"store_url": store_url, "seller_name": seller_name or domain.split(".")[0].title()}
    m_tmp = dict(cfg)
    # 1. Store API reachable + a quote works
    rates, currency = [], None
    try:
        w = Woo(m_tmp); tok = w.new_cart()
        prods = requests.get(f"{store_url}/wp-json/wc/store/v1/products?per_page=5", headers=UA, timeout=20).json()
        pid = None
        for p in prods:
            if p.get("is_purchasable") and p.get("is_in_stock"):
                pid = (p.get("variations") or [{}])[0].get("id") or p["id"]; break
        if pid:
            w.add_items(tok, [{"id": pid, "quantity": 1}])
            cart = w.set_address(tok, {"line_one": "1 Market St", "city": "San Francisco", "state": "CA", "postal_code": "94105", "country": "US"}, None)
            currency = cart["totals"].get("currency_code")
            rates = [rt for pkg in cart.get("shipping_rates", []) for rt in pkg["shipping_rates"]]
        checks["store_api"] = {"ok": True, "sample_item": pid, "rates": [r["rate_id"] for r in rates], "currency": currency}
    except Exception as e:
        checks["store_api"] = {"ok": False, "error": str(e)[:160]}
    # 2. Woo REST credentials (needed to create orders)
    if woo_ck and woo_cs:
        try:
            r = requests.get(f"{store_url}/wp-json/wc/v3/orders?per_page=1", auth=(woo_ck, woo_cs), headers=UA, timeout=20)
            checks["woo_rest"] = {"ok": r.status_code == 200, "http": r.status_code}
            if r.status_code == 200:
                cfg.update(woo_ck=woo_ck, woo_cs=woo_cs)
        except Exception as e:
            checks["woo_rest"] = {"ok": False, "error": str(e)[:160]}
    else:
        checks["woo_rest"] = {"ok": False, "error": "no keys given (needed to create orders)"}
    # 3. Stripe key valid + charges enabled
    if stripe_secret_key:
        try:
            r = requests.get("https://api.stripe.com/v1/account", auth=(stripe_secret_key, ""), headers=UA, timeout=20)
            a = r.json()
            ok = r.status_code == 200 and a.get("charges_enabled")
            checks["stripe"] = {"ok": bool(ok), "account": a.get("id"), "country": a.get("country"), "charges_enabled": a.get("charges_enabled"),
                                "livemode": not stripe_secret_key.startswith("sk_test_"), "error": (a.get("error") or {}).get("message")}
            if r.status_code == 200:
                cfg["stripe_secret_key"] = stripe_secret_key
        except Exception as e:
            checks["stripe"] = {"ok": False, "error": str(e)[:160]}
    else:
        checks["stripe"] = {"ok": False, "error": "no key given"}
    # 4. Policies (checkout gate) + shipping/returns pages
    pol = feed_engine.discover_policies(domain)
    checks["policies"] = {"ok": bool(pol.get("seller_privacy_policy") and pol.get("seller_tos")), **pol}
    cfg["privacy_url"], cfg["tos_url"] = pol.get("seller_privacy_policy", ""), pol.get("seller_tos", "")
    ship_text = _store_policy_text(store_url)
    try:
        pages = requests.get(f"{store_url}/wp-json/wp/v2/pages?per_page=100&_fields=slug,link", headers=UA, timeout=20).json()
        for pg in pages if isinstance(pages, list) else []:
            if any(k in (pg.get("slug") or "") for k in ("shipping", "return", "refund")):
                cfg["policies_url"] = pg["link"]; break
    except Exception:
        pass
    # 5. Shipping map (LLM over the store's own policy text)
    cfg["shipping"] = _llm_shipping_map(store_url, rates, ship_text)
    checks["shipping_map"] = {"ok": bool(cfg["shipping"]), "rates": cfg["shipping"], "policy_text_found": bool(ship_text)}
    cfg["currency"] = currency or "USD"
    cfg["bearer_key"] = "acp_" + secrets.token_urlsafe(32)
    if contact_email:
        cfg["contact_email"] = contact_email
    slug = re.sub(r"[^a-z0-9]+", "", domain.split(".")[0]) or secrets.token_hex(4)
    # never let onboarding overwrite a configured merchant or a different store that already owns this slug
    merchants()   # ensure the static config is loaded before we look at it
    if slug in (_MERCHANTS or {}):   # configured in env/file: onboarding may never touch it
        checks["slug"] = {"ok": False, "error": "this store is managed by CanAIShopYou directly — contact us to change its keys"}
        return (slug, None), checks
    existing = _runtime_merchants().get(slug)
    if existing and existing.get("store_url") != store_url:   # different store, same first label
        slug = slug + "-" + hashlib.sha1(domain.encode()).hexdigest()[:6]
    ready = checks["store_api"].get("ok") and checks["policies"]["ok"]
    return (slug, cfg) if ready else (slug, None), checks

ONBOARD_HTML = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Connect your store to ChatGPT Instant Checkout · CanAIShopYou</title>
<style>body{font-family:-apple-system,system-ui,sans-serif;max-width:640px;margin:40px auto;padding:0 20px;color:#111;line-height:1.45}
label{display:block;margin:14px 0 4px;font-weight:600}input{font-size:16px;padding:10px;width:100%;box-sizing:border-box;border:1px solid #ccc;border-radius:8px}
button{font-size:16px;padding:12px 18px;margin-top:18px;background:#111;color:#fff;border:0;border-radius:8px}.mut{color:#666;font-size:.92em}
pre{background:#f6f6f6;padding:12px;border-radius:8px;overflow:auto;font-size:.85em}.ok{color:#0a7d3c}.bad{color:#b00020}</style>
<h2>Connect your store to ChatGPT Instant Checkout</h2>
<p class=mut>WooCommerce today. We run every check, map your shipping options from your own policy page, and hand you a live checkout endpoint for your ChatGPT merchant application. Keys are stored encrypted on our side and never shown again.</p>
<form method=post>
<label>Store URL</label><input name=store_url placeholder="https://yourstore.com" required>
<label>Store / brand name</label><input name=seller_name placeholder="Your Brand">
<label>Contact email</label><input name=contact_email type=email placeholder="you@yourstore.com">
<label>WooCommerce REST consumer key <span class=mut>(WooCommerce → Settings → Advanced → REST API → Add key, Read/Write)</span></label><input name=woo_ck placeholder="ck_…">
<label>WooCommerce REST consumer secret</label><input name=woo_cs placeholder="cs_…" type=password>
<label>Stripe secret key <span class=mut>(Developers → API keys; a restricted key with PaymentIntents + Refunds write is enough)</span></label><input name=stripe_secret_key placeholder="sk_live_… or rk_live_…" type=password>
<button>Run checks &amp; connect</button></form>
{% if checks %}<h3>Results</h3>
<ul>{% for k, v in checks.items() %}<li><b>{{k}}</b>: <span class="{{'ok' if v.get('ok') else 'bad'}}">{{'OK' if v.get('ok') else 'FAILED'}}</span> <span class=mut>{{ v | tojson }}</span></li>{% endfor %}</ul>
{% if cfg %}<h3 class=ok>Connected: {{slug}}</h3>
<p>Your Agentic Checkout base URL for the OpenAI merchant application:</p><pre>{{base}}/acp/{{slug}}</pre>
<p>Your endpoint bearer key (OpenAI will send it as <code>Authorization: Bearer …</code>). Shown once:</p><pre>{{cfg.bearer_key}}</pre>
<p>Add these two webhooks so order status and refunds stay in sync:</p>
<pre>WooCommerce → Settings → Advanced → Webhooks → Add: topic "Order updated" → {{base}}/acp/{{slug}}/webhooks/woo
Stripe → Developers → Webhooks → Add endpoint: events charge.refunded, charge.dispute.created → {{base}}/acp/{{slug}}/webhooks/stripe</pre>
<p class=mut>Shipping options as we'll present them to ChatGPT (from your policy page): <code>{{cfg.shipping | tojson}}</code>. Email us to adjust.</p>
{% else %}<p class=bad>Not connected yet — fix the failed checks above and run again.</p>{% endif %}{% endif %}"""

@bp.route("/acp/onboard", methods=["GET", "POST"])
def onboard():
    if request.method == "GET":
        return render_template_string(ONBOARD_HTML, checks=None)
    f = request.form if request.form else (request.get_json(silent=True) or {})
    if not (f.get("store_url") or "").strip():
        return render_template_string(ONBOARD_HTML, checks={"store_url": {"ok": False, "error": "required"}}, cfg=None)
    ip = _client_ip()
    if not _rate_ok(f"onboard:{ip}") or not _rate_ok("onboard:global"):
        return "Too many attempts, try again later.", 429
    (slug, cfg), checks = onboard_merchant(f.get("store_url"), f.get("woo_ck") or None, f.get("woo_cs") or None,
                                           f.get("stripe_secret_key") or None, f.get("seller_name") or None, f.get("contact_email") or None)
    if cfg:
        # persist at runtime (app data file); secrets never echoed except the bearer key, once
        try:
            df = os.environ.get("DATA_FILE", "/tmp/cani_data.json")
            d = json.load(open(df)) if os.path.exists(df) else {}
            d.setdefault("acp_merchants", {})[slug] = dict(cfg, onboarded=_now(), source_ip=ip)
            json.dump(d, open(df, "w"))
        except Exception as e:
            import sys; print(f"[acp] onboard persist failed: {e}", file=sys.stderr)
        try:
            from app import log_lead
            log_lead(cfg["store_url"], "", cfg.get("contact_email", ""), extra=f"acp onboard {slug}", kind="acp")
        except Exception:
            pass
    if request.is_json:
        return jsonify({"slug": slug, "connected": bool(cfg), "checks": checks,
                        "endpoint": f"{os.environ.get('ACP_PUBLIC_BASE', 'https://canaishopyou.com')}/acp/{slug}" if cfg else None,
                        "bearer_key": cfg["bearer_key"] if cfg else None, "shipping": cfg["shipping"] if cfg else None})
    return render_template_string(ONBOARD_HTML, checks=checks, cfg=cfg, slug=slug,
                                   base=os.environ.get("ACP_PUBLIC_BASE", "https://canaishopyou.com"))

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
