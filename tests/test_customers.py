"""
Tests for the paying-customer record: /thanks verification + /admin/customers (Stripe as source of truth).

No network: `app.requests` is replaced with FakeStripe, which serves canned Checkout Session JSON for
GET /v1/checkout/sessions (list, paginated) and GET /v1/checkout/sessions/<id>. tests/conftest.py already
sets DATA_FILE/ACP env before `app` is imported; each test here gets its own DATA_FILE and FEEDS_DIR.
"""
import json
import os
from urllib.parse import parse_qs, urlparse

import pytest

import app as app_module

ADMIN_KEY = "admin-secret-for-tests"
STRIPE_KEY = "rk_test_fake"


def session(sid, domain, email="ada@example.com", plan="launch", paid=True, status="complete",
            sub_status="trialing", created=1_756_800_000, amount=19900, expand_sub=True):
    sub = {"id": "sub_" + sid[3:], "object": "subscription", "status": sub_status, "trial_end": created + 30 * 86400}
    return {
        "id": sid, "object": "checkout.session", "mode": "subscription", "status": status,
        "payment_status": "paid" if paid else "unpaid", "amount_total": amount, "currency": "usd",
        "created": created, "customer": "cus_" + sid[3:], "customer_email": email,
        "customer_details": {"email": email},
        "metadata": {"domain": domain, "email": email, "plan": plan},
        "subscription": sub if expand_sub else sub["id"],
    }


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class FakeStripe:
    """Drop-in for the `requests` module surface app.py uses for Stripe (get/post with auth=(key, ''))."""
    def __init__(self, sessions=(), error=None):
        self.sessions = {s["id"]: s for s in sessions}
        self.order = [s["id"] for s in sessions]      # Stripe lists newest first; we hand back in the given order
        self.error = error                            # (http status, message) -> every call fails with it
        self.calls = []

    def get(self, url, params=None, auth=None, timeout=None, **kw):
        u = urlparse(url)
        params = dict(params or {}, **{k: v[0] for k, v in parse_qs(u.query).items()})
        self.calls.append(("GET", u.path, params, auth))
        assert auth == (STRIPE_KEY, ""), "Stripe calls must use basic auth with the secret key"
        if self.error:
            st, msg = self.error
            return FakeResponse(st, {"error": {"type": "invalid_request_error", "message": msg}})
        if u.path == "/v1/checkout/sessions":
            page = int(params.get("limit", "10"))
            ids = self.order
            if params.get("starting_after"):
                ids = ids[ids.index(params["starting_after"]) + 1:]
            data = [self.sessions[i] for i in ids[:page]]
            if params.get("expand[]") != "data.subscription":   # unexpanded: subscription is just the id
                data = [dict(s, subscription=s["subscription"]["id"]) for s in data]
            return FakeResponse(200, {"object": "list", "data": data, "has_more": len(ids) > page})
        if u.path.startswith("/v1/checkout/sessions/"):
            sid = u.path.rsplit("/", 1)[1]
            if sid in self.sessions:
                s = self.sessions[sid]
                if params.get("expand[]") != "subscription":
                    s = dict(s, subscription=s["subscription"]["id"])
                return FakeResponse(200, s)
            return FakeResponse(404, {"error": {"type": "invalid_request_error", "message": f"No such checkout.session: '{sid}'"}})
        raise AssertionError(f"unexpected Stripe call GET {url}")

    def post(self, url, data=None, auth=None, timeout=None, **kw):
        raise AssertionError(f"unexpected Stripe call POST {url}")


class FakeFeedEngine:
    def __init__(self, feeds_dir):
        self.feeds_dir, self.runs = feeds_dir, []

    def run(self, domain, outdir=None, **kw):
        self.runs.append(domain)
        for ext in ("tsv", "tsv.gz", "csv.gz"):
            open(os.path.join(outdir or self.feeds_dir, f"{domain}.{ext}"), "w").write("x")
        return {"ok": True, "domain": domain}


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Isolated DATA_FILE + FEEDS_DIR + fake feed engine; Stripe key set, admin key set. Returns a state holder."""
    data_file = tmp_path / "cani_data.json"
    feeds = tmp_path / "feeds"
    feeds.mkdir()
    monkeypatch.setattr(app_module, "DATA_FILE", str(data_file))
    monkeypatch.setattr(app_module, "FEEDS_DIR", str(feeds))
    fe = FakeFeedEngine(str(feeds))
    monkeypatch.setattr(app_module, "_fe", fe)
    monkeypatch.setenv("STRIPE_SECRET_KEY", STRIPE_KEY)
    monkeypatch.setenv("ACP_ADMIN_KEY", ADMIN_KEY)
    app_module.app.config["TESTING"] = True

    class State:
        pass
    st = State()
    st.data_file, st.feeds, st.fe = data_file, feeds, fe

    def use(stripe):
        monkeypatch.setattr(app_module, "requests", stripe)
        st.stripe = stripe
        return stripe
    st.use = use
    st.data = lambda: json.loads(data_file.read_text()) if data_file.exists() else {}
    use(FakeStripe())
    return st


@pytest.fixture
def web(env):
    return app_module.app.test_client()


# ============================================================================ admin gate
def test_admin_customers_404_when_admin_key_unset(web, env, monkeypatch):
    monkeypatch.delenv("ACP_ADMIN_KEY", raising=False)
    assert web.get(f"/admin/customers?key={ADMIN_KEY}").status_code == 404
    assert web.get("/admin/customers").status_code == 404
    assert env.stripe.calls == [], "no Stripe call before the key check passes"


def test_admin_customers_404_on_missing_or_wrong_key(web, env):
    assert web.get("/admin/customers").status_code == 404
    assert web.get("/admin/customers?key=").status_code == 404
    assert web.get("/admin/customers?key=nope").status_code == 404
    assert web.get(f"/admin/customers?key={ADMIN_KEY}x").status_code == 404
    assert web.get(f"/admin/customers?key={ADMIN_KEY[:-1]}").status_code == 404
    assert env.stripe.calls == []


# ============================================================================ admin happy path
def test_admin_customers_lists_paid_sessions_and_merges_into_data_file(web, env):
    env.use(FakeStripe([
        session("cs_new1", "boka.com", email="ops@boka.com", plan="ads", created=1_756_900_000, amount=49500),
        session("cs_open", "pending.com", paid=False, status="open"),                       # abandoned checkout: excluded
        session("cs_old1", "gomacro.com", email="hi@gomacro.com", created=1_756_800_000),
    ]))
    # a feed already on disk for gomacro.com -> "feed built: yes"
    (env.feeds / "gomacro.com.tsv").write_text("x")

    r = web.get(f"/admin/customers?key={ADMIN_KEY}")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "2 customers" in html
    assert "boka.com" in html and "gomacro.com" in html and "pending.com" not in html
    assert "ops@boka.com" in html and "USD 495.00" in html and "USD 199.00" in html
    assert "trialing" in html and "trial ends" in html
    assert "cs_new1" in html and "cs_old1" in html
    assert html.index("boka.com") < html.index("gomacro.com"), "newest paid session first"
    # feed column: gomacro yes, boka no
    assert html.count("<span class='PASS'>yes</span>") == 1 and html.count("<span class='WARN'>no</span>") == 1

    # Stripe was paginated with the documented params and expand
    lists = [c for c in env.stripe.calls if c[1] == "/v1/checkout/sessions"]
    assert lists and lists[0][2]["limit"] == "100" and lists[0][2]["expand[]"] == "data.subscription"

    # merged into DATA_FILE["customers"] keyed by domain with the full record shape
    custs = env.data()["customers"]
    assert set(custs) == {"boka.com", "gomacro.com"}
    b = custs["boka.com"]
    assert b["session_id"] == "cs_new1" and b["subscription_id"] == "sub_new1" and b["customer_id"] == "cus_new1"
    assert b["amount_total"] == 49500 and b["plan"] == "ads" and b["status"] == "ads_paid"
    assert b["email"] == "ops@boka.com" and b["ts"] == 1_756_900_000 and b["recorded_ts"]
    assert b["subscription_status"] == "trialing" and b["trial_end"] == 1_756_900_000 + 30 * 86400
    assert b["feed_built"] is False and b["feeds"]["tsv"] is False
    assert custs["gomacro.com"]["feed_built"] is True
    # self-heal: both domains now count as built-by-us so /feeds/<domain> rebuilds them after a redeploy
    assert set(env.data()["feeds_built"]) >= {"boka.com", "gomacro.com"}


def test_admin_customers_paginates_with_starting_after(web, env):
    sess = [session(f"cs_p{i:03d}", f"store{i}.com", created=2_000_000_000 - i) for i in range(250)]
    env.use(FakeStripe(sess))
    r = web.get(f"/admin/customers?key={ADMIN_KEY}&format=json")
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is True and j["count"] == 250 and j["source"] == "stripe"
    lists = [c for c in env.stripe.calls if c[1] == "/v1/checkout/sessions"]
    assert len(lists) == 3
    assert "starting_after" not in lists[0][2]
    assert lists[1][2]["starting_after"] == "cs_p099" and lists[2][2]["starting_after"] == "cs_p199"


def test_admin_customers_json_view(web, env):
    env.use(FakeStripe([session("cs_j1", "boka.com")]))
    r = web.get(f"/admin/customers?key={ADMIN_KEY}&format=json")
    assert r.status_code == 200 and r.mimetype == "application/json"
    j = r.get_json()
    assert j["count"] == 1 and j["error"] == ""
    [c] = j["customers"]
    for k in ("domain", "email", "plan", "session_id", "subscription_id", "customer_id", "amount_total",
              "currency", "payment_status", "subscription_status", "trial_end", "ts", "feeds", "feed_built"):
        assert k in c, k
    assert c["domain"] == "boka.com" and c["session_id"] == "cs_j1"


def test_admin_customers_stripe_error_is_shown_not_500(web, env):
    env.data_file.write_text(json.dumps({"scans": [], "leads": [], "customers": {
        "local.com": {"email": "l@local.com", "session_id": "cs_local", "ts": 1_756_000_000, "plan": "launch", "status": "launch_paid"}}}))
    env.use(FakeStripe(error=(401, "Invalid API Key provided: rk_test_***")))
    r = web.get(f"/admin/customers?key={ADMIN_KEY}")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Stripe error: Invalid API Key provided" in html
    assert "local.com" in html, "local record still listed when Stripe is down"
    j = web.get(f"/admin/customers?key={ADMIN_KEY}&format=json").get_json()
    assert j["ok"] is False and j["source"] == "local" and j["count"] == 1


def test_admin_customers_without_stripe_key_shows_local_only(web, env, monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    r = web.get(f"/admin/customers?key={ADMIN_KEY}")
    assert r.status_code == 200
    assert "STRIPE_SECRET_KEY is not set" in r.get_data(as_text=True)
    assert env.stripe.calls == []


# ============================================================================ /thanks
def test_thanks_unpaid_session_shows_no_success_and_records_nothing(web, env):
    env.use(FakeStripe([session("cs_unpaid", "boka.com", paid=False, status="open", sub_status="incomplete")]))
    r = web.get("/thanks?sid=cs_unpaid")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "PAYMENT NOT CONFIRMED" in html and "PAYMENT RECEIVED" not in html
    assert "Welcome aboard" not in html and "boka.com" not in html
    assert "customers" not in env.data() or env.data()["customers"] == {}
    assert env.fe.runs == [], "no feed build for an unpaid session"


def test_thanks_ignores_extra_query_params_and_unknown_or_malformed_sid(web, env):
    env.use(FakeStripe([session("cs_ok", "boka.com")]))
    # domain/plan/paid in the query string must be ignored entirely; a bogus sid never reaches Stripe
    for q in ("", "sid=", "sid=cs_missing", "sid=../x", "sid=cs_ok%27", "domain=evil.com&paid=1&plan=ads"):
        r = web.get(f"/thanks?{q}")
        assert r.status_code == 200
        assert "PAYMENT NOT CONFIRMED" in r.get_data(as_text=True), q
        assert "evil.com" not in r.get_data(as_text=True)
    assert all(c[1] == "/v1/checkout/sessions/cs_missing" for c in env.stripe.calls), "only the well-formed sid hit Stripe"
    assert env.data().get("customers", {}) == {}


def test_thanks_paid_session_records_once_and_builds_feed(web, env):
    env.use(FakeStripe([session("cs_paid1", "boka.com", email="ops@boka.com", plan="launch", amount=19900)]))
    r = web.get("/thanks?sid=cs_paid1&domain=evil.com&plan=ads")   # extra params are not trusted
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "PAYMENT RECEIVED" in html and "Welcome aboard, boka.com" in html and "evil.com" not in html
    assert "ads.openai.com" not in html, "plan comes from Stripe metadata (launch), not the query string"
    assert env.fe.runs == ["boka.com"]
    # Stripe was asked to expand the subscription so status/trial land in the record
    assert env.stripe.calls[0][1] == "/v1/checkout/sessions/cs_paid1" and env.stripe.calls[0][2]["expand[]"] == "subscription"

    d = env.data()
    c = d["customers"]["boka.com"]
    assert c["session_id"] == "cs_paid1" and c["subscription_id"] == "sub_paid1" and c["customer_id"] == "cus_paid1"
    assert c["amount_total"] == 19900 and c["plan"] == "launch" and c["domain"] == "boka.com"
    assert c["email"] == "ops@boka.com" and c["ts"] == 1_756_800_000 and c["status"] == "launch_paid"
    assert c["subscription_status"] == "trialing" and c["payment_status"] == "paid"
    assert c["feed_built"] is True and c["feeds"]["tsv"] is True and c["feeds"]["google.tsv"] is False
    assert "boka.com" in d["feeds_built"]
    paid_leads = [l for l in d["leads"] if l["kind"] == "paid"]
    assert len(paid_leads) == 1 and "cs_paid1" in paid_leads[0]["extra"]
    first_recorded = c["recorded_ts"]

    # refresh of the same /thanks URL: idempotent — no second lead, no second feed build, record kept
    r2 = web.get("/thanks?sid=cs_paid1")
    assert "PAYMENT RECEIVED" in r2.get_data(as_text=True)
    d2 = env.data()
    assert len([l for l in d2["leads"] if l["kind"] == "paid"]) == 1
    assert len(d2["customers"]) == 1 and d2["customers"]["boka.com"]["session_id"] == "cs_paid1"
    assert d2["customers"]["boka.com"]["recorded_ts"] == first_recorded
    assert env.fe.runs == ["boka.com"]


def test_thanks_ads_plan_shows_sponsored_step(web, env):
    env.use(FakeStripe([session("cs_ads1", "boka.com", plan="ads", amount=49500)]))
    html = web.get("/thanks?sid=cs_ads1").get_data(as_text=True)
    assert "PAYMENT RECEIVED" in html and "ads.openai.com" in html
    assert env.data()["customers"]["boka.com"]["plan"] == "ads"


def test_thanks_complete_but_unpaid_is_not_success(web, env):
    # status=complete alone is not enough: the old code accepted this; setup fee must actually be paid
    s = session("cs_cmp", "boka.com", paid=False, status="complete", sub_status="incomplete")
    env.use(FakeStripe([s]))
    html = web.get("/thanks?sid=cs_cmp").get_data(as_text=True)
    assert "PAYMENT NOT CONFIRMED" in html
    assert env.data().get("customers", {}) == {}


def test_thanks_full_promo_no_payment_required_with_live_subscription_is_success(web, env):
    s = session("cs_promo", "boka.com", amount=0, sub_status="trialing")
    s["payment_status"] = "no_payment_required"
    env.use(FakeStripe([s]))
    html = web.get("/thanks?sid=cs_promo").get_data(as_text=True)
    assert "PAYMENT RECEIVED" in html
    assert env.data()["customers"]["boka.com"]["amount_total"] == 0


def test_thanks_stripe_down_shows_no_success(web, env):
    env.use(FakeStripe(error=(500, "boom")))
    html = web.get("/thanks?sid=cs_any").get_data(as_text=True)
    assert "PAYMENT NOT CONFIRMED" in html
    assert env.data().get("customers", {}) == {}


# ============================================================================ self-heal after a redeploy
def test_admin_backfills_customer_recorded_by_thanks_after_data_file_wipe(web, env):
    env.use(FakeStripe([session("cs_h1", "boka.com")]))
    web.get("/thanks?sid=cs_h1")
    assert env.data()["customers"]["boka.com"]["session_id"] == "cs_h1"
    env.data_file.unlink()                      # "redeploy": /tmp wiped
    assert env.data() == {}
    web.get(f"/admin/customers?key={ADMIN_KEY}")
    c = env.data()["customers"]["boka.com"]
    assert c["session_id"] == "cs_h1" and c["subscription_id"] == "sub_h1" and c["amount_total"] == 19900


def test_upsert_keeps_newest_session_per_domain_and_remembers_older_ones(web, env):
    env.use(FakeStripe([
        session("cs_second", "boka.com", plan="ads", created=1_756_900_000, amount=49500),
        session("cs_first", "boka.com", plan="launch", created=1_756_800_000, amount=19900),
    ]))
    j = web.get(f"/admin/customers?key={ADMIN_KEY}&format=json").get_json()
    assert j["count"] == 1
    [c] = j["customers"]
    assert c["session_id"] == "cs_second" and c["plan"] == "ads" and c["previous_sessions"] == ["cs_first"]
    # a later /thanks hit on the older session must not demote the record
    web.get("/thanks?sid=cs_first")
    c = env.data()["customers"]["boka.com"]
    assert c["session_id"] == "cs_second" and c["previous_sessions"] == ["cs_first"]
