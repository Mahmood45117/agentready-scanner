"""
Offline tests for feed_engine.py — no network. `feed_engine._get` (the single HTTP primitive every adapter,
policy probe and diagnosis goes through) is replaced by FakeNet, a tiny router of URL-regex -> (status, body).

Each test pins a bug found while hardening against 14 real stores (3 Sep 2026):
  Shopify  : allbirds, brooklinen, deathwishcoffee, gymshark, skullcandy, bombas (bot wall), burrow (headless)
  Woo      : linealprints, secretaardvark, tarptent, offermanwoodshop, porterandyork, nalgene, boostoxygen,
             smilebrilliant, wildwoodgrilling (REST locked)
"""
import json
import os
import re
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import feed_engine as fe  # noqa: E402


# --------------------------------------------------------------------------- fake network
class FakeNet:
    """Routes: list of (regex, status, body). Body may be a dict/list (JSON), a str (HTML) or an Exception
    marker string 'timeout' / 'connection'. Records every URL fetched in .calls."""

    def __init__(self, routes=()):
        self.routes = list(routes)
        self.calls = []

    def add(self, pattern, status=200, body=None, final_url=None):
        self.routes.append((pattern, status, body, final_url))
        return self

    def __call__(self, url, timeout=15, as_json=True, meta=None):
        self.calls.append(url)
        m = meta if meta is not None else {}
        for pattern, status, body, final_url in self.routes:
            if re.search(pattern, url):
                if body == "timeout":
                    m["error"] = "timeout"; return None
                if body == "connection":
                    m["error"] = "connection"; return None
                text = body if isinstance(body, str) else json.dumps(body)
                m.update(status=status, final_url=final_url or url, body=text[:800],
                         content_type="text/html" if isinstance(body, str) else "application/json")
                if status != 200:
                    return None
                if as_json:
                    if isinstance(body, str):
                        m["error"] = "not_json"; return None
                    return json.loads(json.dumps(body))  # fresh copy: adapters mutate in place
                return text
        m.update(status=404, final_url=url, body="", content_type="text/html")
        return None


@pytest.fixture
def net(monkeypatch):
    n = FakeNet()
    monkeypatch.setattr(fe, "_get", n)
    return n


# --------------------------------------------------------------------------- catalog fixtures
def shopify_product(pid=1, title="Wool Runner", images=1, variants=None, body="<p>Soft &amp; warm.</p>", vendor="Allbirds"):
    vs = variants or [{"id": pid * 100, "title": "Default Title", "option1": "Default Title", "price": "98.00",
                       "compare_at_price": None, "available": True, "sku": f"SKU{pid}", "grams": 454,
                       "requires_shipping": True, "featured_image": None}]
    return {"id": pid, "title": title, "handle": f"p{pid}", "body_html": body, "vendor": vendor, "product_type": "Shoes",
            "tags": "a, b", "options": [{"name": "Size"}], "variants": vs, "_brand": vendor, "_category": "Shoes",
            "images": [{"src": f"https://cdn.shopify.com/{pid}-{i}.jpg"} for i in range(images)]}


def woo_product(pid=10, name="Poster", variations=(), price="3900", regular="3900", sale="3900", images=1, **extra):
    p = {"id": pid, "name": name, "slug": f"p{pid}", "type": "variable" if variations else "simple", "permalink": f"https://shop.test/product/p{pid}/",
         "description": "<p>Museum-grade matte paper.</p>", "short_description": "", "sku": f"W{pid}", "is_purchasable": True,
         "is_in_stock": True, "is_on_backorder": False, "is_password_protected": False,
         "prices": {"price": price, "regular_price": regular, "sale_price": sale, "currency_code": "USD", "currency_minor_unit": 2},
         "images": [{"src": f"https://shop.test/img{pid}-{i}.jpg"} for i in range(images)],
         "categories": [{"name": "Prints", "slug": "prints"}], "tags": [], "brands": [], "average_rating": "0", "review_count": 0,
         "attributes": [{"name": "Size", "has_variations": True, "terms": [{"name": "A"}, {"name": "B"}]},
                        {"name": "Paper finish", "has_variations": False, "terms": [{"name": "Matte"}]}],
         "variations": [{"id": vid, "attributes": [{"name": "Size", "value": val}]} for vid, val in variations],
         "weight": "0.48", "formatted_weight": "0.48 lbs", "dimensions": {}, "formatted_dimensions": "N/A"}
    p.update(extra)
    return p


def woo_variation(vid, parent, value="A", price="3900", sku=None, in_stock=True, weight="0.48", fw="0.48 lbs", dims=None, fd="N/A", backorder=False):
    return {"id": vid, "parent": parent, "type": "variation", "sku": sku or f"V{vid}", "description": "",
            "prices": {"price": price, "regular_price": price, "sale_price": price, "currency_code": "USD", "currency_minor_unit": 2},
            "is_in_stock": in_stock, "is_on_backorder": backorder, "weight": weight, "formatted_weight": fw,
            "dimensions": dims or {}, "formatted_dimensions": fd, "images": [{"src": f"https://shop.test/v{vid}.jpg"}],
            "attributes": [{"name": "Size", "value": value}]}


def policies_ok(net, host="shop.test"):
    net.add(rf"https://{host}/privacy-policy/$", 200, "<html><title>Privacy</title></html>")
    net.add(rf"https://{host}/terms-of-service/$", 200, "<html><title>Terms</title></html>")
    net.add(rf"https://{host}/returns-refunds/$", 200, "<html><title>Returns</title></html>")


# =========================================================================== text + title cleaning
def test_txt_strips_style_script_entities_and_double_encoded_html():
    raw = "<style>.x{color:red}</style><p>Get in, it&#8217;s adventure time &#8211; made in USA</p>&lt;b&gt;bold&lt;/b&gt;<script>x()</script>"
    out = fe._txt(raw)
    assert out == "Get in, it's adventure time – made in USA bold"
    assert "\n" not in fe._txt("a\n\n\tb") and fe._txt("a\n\n\tb") == "a b"


def test_txt_keeps_lineal_entity_mapping_byte_identical():
    # the historical mapping (&#8217; -> ASCII apostrophe) must not change or the committed Lineal feed drifts
    assert fe._txt("Lineal&#8217;s &quot;Thread&quot; &amp; Orbit&nbsp;print") == "Lineal's \"Thread\" & Orbit print"


def test_clean_title_recases_all_caps_but_keeps_acronyms_and_numbers():
    assert fe._clean_title("EXTRA NIGHT GUARD TRAY (3MM)") == ("Extra Night Guard Tray (3MM)", True)
    assert fe._clean_title("USB LED LAMP XL") == ("USB LED Lamp XL", True)
    assert fe._clean_title("Series 01 — No. 3") == ("Series 01 — No. 3", False)
    assert fe._clean_title("  Wool &amp; Cotton  Runner ") == ("Wool & Cotton Runner", False)


# =========================================================================== validate
def test_validate_literal_angle_brackets_are_not_html_but_tags_are():
    base = {f: "" for f in fe.FIELDS}
    base.update(is_eligible_search="true", is_eligible_checkout="false", item_id="1", title="Knee sleeve", url="https://s.test/p",
                brand="B", image_url="https://s.test/i.jpg", price="10.00 USD", availability="in_stock", seller_name="S", target_countries="US")
    ok = dict(base, description="S: >20cm. XXL: <39cm.")            # gymshark size table, valid text
    bad = dict(base, item_id="2", description="a <b>bold</b> claim")
    v = fe.validate([ok, bad])
    assert [e for e in v["errors"] if "HTML" in e] == ["row 1: description contains HTML"]


def test_validate_zero_price_and_malformed_sale_price_do_not_crash():
    base = {f: "" for f in fe.FIELDS}
    base.update(is_eligible_search="true", is_eligible_checkout="false", item_id="1", title="T", description="d", url="https://s.test/p",
                brand="B", image_url="https://s.test/i.jpg", availability="in_stock", seller_name="S", target_countries="US")
    v = fe.validate([dict(base, price="0 USD"), dict(base, item_id="2", price="10.00 USD", sale_price="oops")])
    assert any("above 0" in e for e in v["errors"]) and any("sale_price 'oops'" in e for e in v["errors"])


# =========================================================================== build_rows
def test_rows_without_image_or_price_are_skipped_and_counted_not_emitted():
    products = [
        shopify_product(1, "Content: Labor Day Sale", images=0, variants=[{"id": 11, "title": "Default Title", "price": "0.00", "available": True}]),
        shopify_product(2, "Towel Set", images=0),                                            # real product, no images
        shopify_product(3, "Gift", variants=[{"id": 31, "title": "Default Title", "price": "", "available": True}]),
        shopify_product(4, "Runner"),
    ]
    rows, issues, _ = fe.build_rows("s.test", products, "B", "USD", {})
    assert [r["item_id"] for r in rows] == ["400"]
    assert issues["skipped_no_image"] == 2 and issues["no_image"] == 2 and issues["skipped_no_price"] == 1
    assert fe.validate(rows)["error_count"] == 0


def test_price_normalisation_thousands_separators_and_decimal_comma():
    def one(price, cmp=None):
        p = shopify_product(1, variants=[{"id": 11, "title": "Default Title", "price": price, "compare_at_price": cmp, "available": True}])
        return fe.build_rows("s.test", [p], "B", "EUR", {})[0][0]
    assert one("$1,299.00")["price"] == "1299.00 EUR"
    assert one("29,90")["price"] == "29.90 EUR"
    r = one("39.00", "49.00")
    assert (r["price"], r["sale_price"]) == ("49.00 EUR", "39.00 EUR")
    assert one("39.00", "0.00")["sale_price"] == ""            # Shopify's compare_at 0.00 is "no sale"


def test_zero_weight_and_zero_dimensions_are_not_emitted():
    v = {"id": 11, "title": "Default Title", "price": "5.00", "available": True, "_weight": "0", "_weight_unit": "lb",
         "_dims": {"length": "0", "width": "0", "height": "0"}}
    r = fe.build_rows("s.test", [shopify_product(1, variants=[v])], "B", "USD", {})[0][0]
    assert r["weight"] == "" and r["item_weight_unit"] == "" and r["length"] == "" and r["dimensions_unit"] == ""
    assert fe.validate([r])["error_count"] == 0


def test_shopify_variant_featured_image_digital_flag_and_variant_url():
    vs = [{"id": 11, "title": "Black", "option1": "Black", "price": "10.00", "available": True, "requires_shipping": False,
           "featured_image": {"src": "https://cdn.shopify.com/black.jpg"}, "grams": 0},
          {"id": 12, "title": "White", "option1": "White", "price": "10.00", "available": False, "requires_shipping": True,
           "featured_image": None, "grams": 100}]
    p = shopify_product(1, variants=vs)
    p["options"] = [{"name": "Color"}]
    fe_products = [p]
    # pull_shopify normalisation happens on the wire shape; emulate it
    for v in p["variants"]:
        if v.get("featured_image"):
            v["_image"] = v["featured_image"]["src"]
        if v.get("requires_shipping") is False:
            v["_digital"] = True
    rows = fe.build_rows("s.test", fe_products, "B", "USD", {}, site="www.s.test")[0]
    assert rows[0]["image_url"] == "https://cdn.shopify.com/black.jpg" and rows[0]["is_digital"] == "true"
    assert rows[1]["image_url"] == "https://cdn.shopify.com/1-0.jpg" and rows[1]["is_digital"] == "false"
    assert rows[0]["url"] == "https://www.s.test/products/p1?variant=11" and rows[0]["color"] == "Black"
    assert rows[1]["availability"] == "out_of_stock"


def test_description_fallback_is_a_sentence_and_titles_are_cleaned():
    p = shopify_product(1, "SUPER PLUSH TOWEL", body="", vendor="Brooklinen")
    r, issues, _ = fe.build_rows("s.test", [p], "B", "USD", {})
    assert r[0]["title"] == "Super Plush Towel" and issues["titles_recased"] == 1
    assert r[0]["description"] == "Super Plush Towel by Brooklinen Category: Shoes." and issues["no_description"] == 1
    assert fe.build_rows("s.test", [shopify_product(2, "")], "B", "USD", {})[1]["skipped_no_title"] == 1


def test_variant_url_appends_with_ampersand_when_url_has_query():
    prod = {"id": 10, "title": "Poster", "handle": "p10", "body_html": "x", "_url": "https://shop.test/?product=p10",
            "images": [{"src": "https://shop.test/i.jpg"}], "options": [{"name": "Size"}],
            "variants": [{"id": 1, "title": "A", "option1": "A", "price": "1.00", "available": True},
                         {"id": 2, "title": "B", "option1": "B", "price": "1.00", "available": True}]}
    rows = fe.build_rows("shop.test", [prod], "B", "USD", {})[0]
    assert rows[0]["url"] == "https://shop.test/?product=p10&variant=1"


# =========================================================================== Shopify adapter
def test_pull_shopify_paginates_and_flags_truncation(net):
    page = {"products": [shopify_product(i) for i in range(250)]}
    net.add(r"/products\.json\?limit=250&page=\d+", 200, page, final_url="https://www.s.test/products.json")
    info = {}
    products = fe.pull_shopify("s.test", max_pages=2, info=info)
    assert len(products) == 500 and info["truncated"] == 500 and info["site"] == "www.s.test"
    v = products[0]["variants"][0]
    assert v["_weight"] == "1.00" and v["_weight_unit"] == "lb"


def test_pull_shopify_bot_wall_html_is_not_a_catalog(net):
    net.add(r"/products\.json", 429, "<html>Just a moment...</html>")
    info = {}
    assert fe.pull_shopify("bombas.com", info=info) == [] and info["meta"]["status"] == 429


def test_shopify_meta_prefers_meta_json_then_cart_js(net):
    net.add(r"/meta\.json", 200, {"name": "Allbirds", "currency": "usd", "country": "US", "domain": "www.allbirds.com"})
    m = fe.shopify_meta("allbirds.com")
    assert m == {"name": "Allbirds", "currency": "USD", "country": "US", "site": "www.allbirds.com"}
    net2 = FakeNet().add(r"/meta\.json", 403, "<html>forbidden</html>").add(r"/cart\.js", 200, {"currency": "GBP"})
    fe_get, fe._get = fe._get, net2
    try:
        assert fe.shopify_meta("x.test")["currency"] == "GBP" and fe.shopify_currency("x.test") == ("GBP", True)
    finally:
        fe._get = fe_get


# =========================================================================== WooCommerce Store API adapter
def test_woo_variations_listed_once_per_product_not_per_variation(net):
    p = woo_product(10, variations=((1, "A"), (2, "B"), (3, "C")))
    net.add(r"/wc/store/v1/products\?per_page=100&page=1$", 200, [p])
    net.add(r"type=variation&parent=10&per_page=100&page=1", 200, [woo_variation(1, 10, "A"), woo_variation(2, 10, "B", price="4900"), woo_variation(3, 10, "C", in_stock=False, backorder=True)])
    info = {}
    products, currency = fe.pull_woo_store("shop.test", info=info)
    assert currency == "USD" and len(products) == 1 and info["incomplete_variants"] == 0
    vs = products[0]["variants"]
    assert [v["price"] for v in vs] == ["39.00", "49.00", "39.00"] and vs[1]["sku"] == "V2"
    assert vs[0]["_weight_unit"] == "lb" and vs[2]["_backorder"] is True
    assert not any(re.search(r"/products/\d+$", u) for u in net.calls), "must not fetch variations one by one"
    rows = fe.build_rows("shop.test", products, "B", currency, {})[0]
    assert rows[2]["availability"] == "backorder" and rows[0]["variant_dict"] == '{"Size": "A"}'
    assert rows[0]["title"] == "Poster - A" and rows[0]["item_weight_unit"] == "lb"


def test_woo_old_store_ignoring_variation_filter_falls_back_to_per_id(net):
    p = woo_product(10, variations=((1, "A"), (2, "B")))
    net.add(r"/wc/store/v1/products\?per_page=100&page=1$", 200, [p])
    net.add(r"type=variation&parent=10", 200, [woo_product(99)])              # filter ignored: ordinary products come back
    net.add(r"/wc/store/v1/products/1$", 200, woo_variation(1, 10, "A", price="1000"))
    net.add(r"/wc/store/v1/products/2$", 200, woo_variation(2, 10, "B", price="2000"))
    products, _ = fe.pull_woo_store("shop.test")
    assert [v["price"] for v in products[0]["variants"]] == ["10.00", "20.00"]


def test_woo_page_timeout_is_retried_once_and_truncation_reported(net):
    calls = {"n": 0}
    first = [woo_product(i) for i in range(1, 101)]

    def page2(url, timeout=15, as_json=True, meta=None):
        calls["n"] += 1
        if calls["n"] == 1:
            meta["error"] = "timeout"; return None
        return [woo_product(200)]
    net.add(r"per_page=100&page=1$", 200, first)
    base = fe._get

    def router(url, timeout=15, as_json=True, meta=None):
        m = meta if meta is not None else {}
        return page2(url, timeout, as_json, m) if "page=2" in url else base(url, timeout, as_json, m)
    fe._get = router
    try:
        info = {}
        products, _ = fe.pull_woo_store("shop.test", info=info)
    finally:
        fe._get = base
    assert len(products) == 101 and calls["n"] == 2 and "truncated" not in info


def test_woo_units_parsed_from_formatted_strings_and_grouped_external_skipped(net):
    p = woo_product(10, variations=((1, "A"),))
    g = woo_product(11, type="grouped"); e = woo_product(12, type="external")
    net.add(r"/products\?per_page=100&page=1$", 200, [p, g, e])
    net.add(r"type=variation&parent=10", 200, [woo_variation(1, 10, weight="2", fw="2 kg", dims={"length": "20", "width": "30", "height": "1"}, fd="20 × 30 × 1 cm")])
    products, _ = fe.pull_woo_store("shop.test")
    assert [x["id"] for x in products] == [10]
    r = fe.build_rows("shop.test", products, "B", "USD", {})[0][0]
    assert (r["weight"], r["item_weight_unit"], r["length"], r["dimensions_unit"]) == ("2", "kg", "20", "cm")
    assert fe._unit_from("0.48 lbs", fe.WEIGHT_UNITS) == "lb" and fe._unit_from("N/A", fe.DIM_UNITS) == ""


def test_woo_minor_units_zero_decimals_currency():
    assert fe._minor("2900", 0) == "2900" and fe._minor("2900", 2) == "29.00" and fe._minor(None, 2) == "0"


def test_woo_budget_exhausted_marks_incomplete_variants_instead_of_hanging(net):
    p = woo_product(10, variations=((1, "A"),))
    net.add(r"per_page=100&page=1$", 200, [p])
    net.add(r"type=variation&parent=10", 200, [woo_variation(1, 10)])
    info = {}
    products, _ = fe.pull_woo_store("shop.test", deadline=time.monotonic() - 1, info=info)
    assert info["incomplete_variants"] == 1 and products[0]["variants"][0]["price"] == "39.00"   # parent fallback
    assert not any("type=variation" in u for u in net.calls)


# =========================================================================== WooCommerce REST (keys) adapter
def test_pull_woocommerce_rest_error_dict_does_not_crash_and_variations_expand(net):
    net.add(r"/wc/v3/products\?per_page=100&page=1", 200, [
        {"id": 5, "name": "Tee", "slug": "tee", "type": "variable", "permalink": "https://shop.test/product/tee/", "price": "20",
         "images": [{"src": "https://shop.test/t.jpg"}], "categories": [{"name": "Apparel"}], "stock_status": "instock",
         "attributes": [{"name": "Size", "variation": True}], "variations": [51, 52]}])
    net.add(r"/wc/v3/products/5/variations", 200, [
        {"id": 51, "price": "20", "regular_price": "25", "sku": "T-S", "stock_status": "instock", "attributes": [{"name": "Size", "option": "S"}], "image": {"src": "https://shop.test/s.jpg"}},
        {"id": 52, "price": "20", "regular_price": "", "sku": "T-M", "stock_status": "outofstock", "attributes": [{"name": "Size", "option": "M"}]}])
    products = fe.pull_woocommerce("shop.test", "ck", "cs")
    rows = fe.build_rows("shop.test", products, "B", "USD", {})[0]
    assert [r["item_id"] for r in rows] == ["51", "52"] and rows[0]["sale_price"] == "20 USD" and rows[0]["price"] == "25 USD"
    assert rows[0]["image_url"] == "https://shop.test/s.jpg" and rows[1]["availability"] == "out_of_stock"
    locked = FakeNet().add(r"/wc/v3/products", 401, {"code": "woocommerce_rest_cannot_view", "message": "Sorry"})
    fe_get, fe._get = fe._get, locked
    try:
        assert fe.pull_woocommerce("shop.test", "ck", "cs") == []
    finally:
        fe._get = fe_get


# =========================================================================== diagnosis (no traceback, actionable)
def _fail_reason(net, domain="shop.test"):
    rep = fe.run(domain, outdir="/nonexistent-never-written", budget=0)
    assert rep["ok"] is False and "reason" in rep and "Traceback" not in rep["reason"]
    return rep["reason"]


def test_reason_locked_wordpress_rest_api(net):
    net.add(r"/products\.json", 404, "404 Not Found")
    net.add(r"/wp-json/wc/store", 401, {"code": "rest_not_logged_in", "message": "You are not currently logged in."})
    r = _fail_reason(net, "wildwoodgrilling.com")
    assert "REST API is locked" in r and "WooCommerce → Settings → Advanced → REST API" in r


def test_reason_bot_wall(net):
    net.add(r".", 429, "<!DOCTYPE html><html>blocked</html>")
    assert "bot protection" in _fail_reason(net, "bombas.com") and "HTTP 429" in _fail_reason(net, "bombas.com")


def test_reason_dns_or_connection_failure(net):
    net.add(r".", 0, "connection")
    assert "couldn't connect" in _fail_reason(net, "no-such-store-xyz.com")


def test_reason_shopify_empty_catalog_and_password(net):
    net.add(r"/products\.json", 200, {"products": []})
    assert "catalog is empty" in _fail_reason(net)
    pw = FakeNet().add(r"/products\.json", 200, {"products": []}, final_url="https://shop.test/password")
    fe_get, fe._get = fe._get, pw
    try:
        assert "password-protected" in _fail_reason(pw)
    finally:
        fe._get = fe_get


def test_reason_platform_detection_from_homepage(net):
    net.add(r"/products\.json", 404, "nope")
    net.add(r"/wp-json/wc/store", 404, "<html>404</html>")
    net.add(r"https://shop\.test/$", 200, '<html><img src="image/png"><script src="https://cdn11.bigcommerce.com/x.js"></script></html>')
    assert "BigCommerce" in _fail_reason(net)
    sq = FakeNet().add(r"/products\.json", 404, "x").add(r"/wp-json", 404, "x").add(r"/$", 200, '<img src="image/x.png"><link href="https://static1.squarespace.com/a.css">')
    fe_get, fe._get = fe._get, sq
    try:
        r = _fail_reason(sq, "squarespace.com")
        assert "Squarespace" in r and "Magento" not in r          # 'image/' used to match the Magento marker 'mage/'
    finally:
        fe._get = fe_get


def test_reason_timeout_mentions_budget(net):
    net.add(r".", 0, "timeout")
    rep = fe.run("slow.test", outdir="/nonexistent", budget=45)
    assert rep["ok"] is False and "longer than 45s" in rep["reason"]


def test_run_rejects_garbage_domain_without_network(net):
    rep = fe.run("not a domain", outdir="/nonexistent")
    assert rep["ok"] is False and "isn't a store domain" in rep["reason"] and net.calls == []
    assert fe.normalize_domain(" HTTPS://www.Store.com/shop?x=1 ") == "www.store.com"
    assert fe.normalize_domain("store.com:443") == "store.com"


# =========================================================================== policies
def test_policies_soft_404_rejected_and_priority_order_kept(net):
    net.add(r"/policies/privacy-policy$", 200, "<html><title>Page not found - Shop</title></html>")   # WP guessed permalink, soft 404
    net.add(r"/privacy/$", 200, "<html><body class='error404'></body></html>")
    net.add(r"/privacy-policy/$", 200, "<html><title>Privacy Policy</title></html>")
    net.add(r"/terms/$", 200, "<html><title>Terms</title></html>")
    net.add(r"/terms-of-service/$", 200, "<html><title>Terms of Service</title></html>")
    net.add(r"/returns/$", 200, "<html><title>Returns</title></html>", final_url="https://shop.test/")        # redirect to homepage
    found = fe.discover_policies("shop.test")
    assert found["seller_privacy_policy"] == "https://shop.test/privacy-policy/"
    assert found["seller_tos"] == "https://shop.test/terms-of-service/"           # earlier candidate wins even though /terms/ also 200
    assert "return_policy" not in found


def test_policies_wp_pages_api_matches_title_and_homepage_footer_fallback(net):
    net.add(r"/wp-json/wp/v2/pages", 200, [{"slug": "legal", "link": "https://shop.test/legal/", "title": {"rendered": "Terms &amp; Conditions"}}])
    net.add(r"https://shop\.test/$", 200, '<html><footer><a href="/privacy-notice/">Privacy</a> <a href="mailto:x@y">Returns</a> '
                                          '<a href="https://other.example/returns">Returns</a> <a href="/help/returns-and-refunds/">Refunds &amp; Returns</a></footer></html>')
    net.add(r"/privacy-notice/$", 200, "<html><title>Privacy Notice</title></html>")
    net.add(r"/help/returns-and-refunds/$", 200, "<html><title>Refunds</title></html>")
    found = fe.discover_policies("shop.test")
    assert found == {"seller_tos": "https://shop.test/legal/", "seller_privacy_policy": "https://shop.test/privacy-notice/",
                     "return_policy": "https://shop.test/help/returns-and-refunds/"}


# =========================================================================== end-to-end run()
def test_run_woo_store_end_to_end_writes_all_feeds_and_report(net, tmp_path):
    p = woo_product(10, name="Thread", variations=((1, "A"), (2, "B")))
    net.add(r"/products\.json", 404, "nope")
    net.add(r"/wc/store/v1/products\?per_page=100&page=1$", 200, [p, woo_product(11, name="Sticker", price="", regular="", sale="")])
    net.add(r"type=variation&parent=10", 200, [woo_variation(1, 10, "A"), woo_variation(2, 10, "B", price="4900")])
    net.add(r"/wp-json/\?_fields=name", 200, {"name": "Lineal Prints", "url": "https://shop.test"})
    policies_ok(net)
    rep = fe.run("https://shop.test/", outdir=str(tmp_path), budget=0)
    assert rep["ok"] and rep["platform"] == "woocommerce" and rep["products"] == 2 and rep["feed_rows"] == 2
    assert rep["checkout_eligible"] is True and rep["currency"] == "USD" and rep["currency_detected"] is True
    assert rep["site_name"] == "Lineal Prints" and rep["spec"]["error_count"] == 0
    assert rep["notes"] == ["1 variant rows skipped (cannot pass the spec): 0 without an image, 1 without a price, 0 without a title"]
    for f in ("shop.test.tsv", "shop.test.tsv.gz", "shop.test.csv.gz", "shop.test.google.tsv", "shop.test.shopify.csv", "shop.test.report.json"):
        assert (tmp_path / f).exists()
    tsv = (tmp_path / "shop.test.tsv").read_text().splitlines()
    assert tsv[0].split("\t") == fe.FIELDS and len(tsv) == 3
    row = dict(zip(fe.FIELDS, tsv[1].split("\t")))
    assert row["seller_name"] == "Lineal Prints" and row["brand"] == "Lineal Prints" and row["price"] == "39.00 USD"
    assert row["url"] == "https://shop.test/product/p10/?variant=1" and row["seller_tos"] == "https://shop.test/terms-of-service/"


def test_run_shopify_store_end_to_end_uses_meta_json(net, tmp_path):
    net.add(r"/products\.json\?limit=250&page=1", 200, {"products": [shopify_product(1), shopify_product(2, "Content: Banner", images=0)]},
            final_url="https://www.allbirds.com/products.json?limit=250&page=1")
    net.add(r"/meta\.json", 200, {"name": "Allbirds", "currency": "CAD", "country": "CA", "domain": "www.allbirds.com"})
    net.add(r"/policies/privacy-policy$", 200, "<html><title>Privacy</title></html>")
    net.add(r"/policies/terms-of-service$", 200, "<html><title>Terms</title></html>")
    rep = fe.run("allbirds.com", outdir=str(tmp_path), budget=0)
    assert rep["ok"] and rep["platform"] == "shopify" and rep["feed_rows"] == 1 and rep["currency"] == "CAD"
    row = json.loads(json.dumps(dict(zip(fe.FIELDS, (tmp_path / "allbirds.com.tsv").read_text().splitlines()[1].split("\t")))))
    assert row["url"] == "https://www.allbirds.com/products/p1" and row["store_country"] == "CA" and row["seller_name"] == "Allbirds"
    assert row["price"] == "98.00 CAD" and row["seller_url"] == "https://www.allbirds.com"


def test_run_all_rows_unusable_is_a_reason_not_an_empty_feed(net, tmp_path):
    net.add(r"/products\.json", 200, {"products": [shopify_product(1, images=0)]})
    net.add(r"/meta\.json", 200, {"name": "X", "currency": "USD"})
    rep = fe.run("x.test", outdir=str(tmp_path), budget=0)
    assert rep["ok"] is False and "none can go in a feed" in rep["reason"] and not list(tmp_path.iterdir())


def test_run_never_raises_even_on_internal_bug(monkeypatch, tmp_path, capsys):
    def boom(url, timeout=15, as_json=True, meta=None):
        raise RuntimeError("simulated bug below _get")
    monkeypatch.setattr(fe, "_get", boom)
    rep = fe.run("x.test", outdir=str(tmp_path))
    assert rep["ok"] is False and "unexpected error" in rep["reason"] and "RuntimeError" in rep["reason"]
    assert "Traceback" in capsys.readouterr().err        # logged for Render, not shown to the prospect


def test_get_swallows_transport_errors_into_meta(monkeypatch):
    class Req:
        Timeout = fe.requests.Timeout
        ConnectionError = fe.requests.ConnectionError

        @staticmethod
        def get(url, **kw):
            raise fe.requests.ConnectionError("dns")
    monkeypatch.setattr(fe, "requests", Req)
    m = {}
    assert fe._get("https://x.test/", meta=m) is None and m["error"] == "connection"
