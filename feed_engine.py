"""
feed_engine.py — store catalog in → OpenAI-spec product feed out (the front half of the ACP gateway).

Implements the OpenAI product feed flat-file schema (developers.openai.com/commerce/specs/feed), full field set:
  required   : item_id, title, description, url, image_url, brand, price, availability, seller_name,
               target_countries, is_eligible_search, is_eligible_checkout
  checkout   : seller_privacy_policy + seller_tos (required when is_eligible_checkout=true)
  recommended: group_id / listing_has_variations / variant_dict / item_group_title, color, material,
               product_category, condition, dimensions (+unit), weight (+unit), age_group, shipping, returns,
               review_count / star_rating, q_and_a, related_product_id / relationship_type, seller_url
  formats    : one variant per row, lowercase underscore headers, UTF-8; .tsv, .tsv.gz and .csv.gz written
               under a STABLE filename (OpenAI wants the same name overwritten on every snapshot)
  delivery   : OpenAI ingests by SFTP push or API after merchant approval (there is no pull-by-URL);
               push_sftp() uploads the snapshot once OpenAI provides credentials.

Adapters: Shopify (public /products.json + /meta.json), WooCommerce Store API (public, keyless — every Woo >= 3.6),
          WooCommerce REST (merchant keys, fallback for stores that disable the Store API).
Per-domain NON-SECRET enrichment (shipping string, returns, category, Q&A…) lives in feed_profiles.json.

Hardening (3 Sep, tested against 14 real stores — see tests/test_feed_engine.py):
  * run() never raises for a store problem: it returns {"ok": False, "reason": <one actionable sentence>} produced by
    diagnose() (locked WP REST API, bot wall, password-protected / empty Shopify, DNS, timeout, BigCommerce/Magento…).
  * Woo variations are listed one request PER PRODUCT (?type=variation&parent=) instead of one per variation —
    byte-identical data, 5-10x fewer requests. A time budget (FEED_TIME_BUDGET, default 90s; the web worker dies at
    120s) bounds the whole build; anything cut short is reported in "notes", never silently.
  * Rows that can never pass the spec (no image, no price, no title) are dropped and counted instead of emitted.
CLI:  python3 feed_engine.py <domain> [--budget SECONDS] [--check-urls N]  -> feeds/<domain>.tsv (+ .tsv.gz, .csv.gz,
      .google.tsv, .shopify.csv) + report JSON.  CLI default budget = unlimited.
"""
import csv, gzip, html as _html, io, json, re, sys, os, time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse
import requests

UA = {"User-Agent": "Mozilla/5.0 (CanAIShopYou feed engine)"}
AVAIL = {True: "in_stock", False: "out_of_stock"}
HERE = os.path.dirname(os.path.abspath(__file__))
CONNECT_TIMEOUT = 8          # seconds to establish a connection; read timeouts are per call
DEFAULT_BUDGET = float(os.environ.get("FEED_TIME_BUDGET", "90"))   # web path; CLI passes 0 (unlimited)

# Column order = spec section order. Every name is a spec field; nothing non-spec is emitted.
FIELDS = [
    # flags
    "is_eligible_search", "is_eligible_checkout", "is_ads_eligible",
    # basic
    "item_id", "gtin", "mpn", "title", "description", "url",
    # item information
    "brand", "condition", "product_category", "material", "length", "width", "height", "dimensions_unit",
    "weight", "item_weight_unit", "age_group",
    # media
    "image_url", "additional_image_urls", "video_url",
    # price
    "price", "sale_price",
    # availability
    "availability", "availability_date",
    # variants
    "group_id", "listing_has_variations", "variant_dict", "item_group_title", "color", "size",
    # fulfillment
    "shipping", "is_digital",
    # merchant
    "seller_name", "seller_url", "seller_privacy_policy", "seller_tos",
    # returns
    "accepts_returns", "return_deadline_in_days", "accepts_exchanges", "return_policy",
    # performance / reviews / related
    "popularity_score", "review_count", "star_rating", "q_and_a", "related_product_id", "relationship_type",
    # geo
    "target_countries", "store_country",
]
REQUIRED = ["is_eligible_search", "is_eligible_checkout", "item_id", "title", "description", "url", "brand",
            "image_url", "price", "availability", "seller_name", "target_countries"]
# what the spec says improves "ranking, relevance, and user trust" — scored in the report
RECOMMENDED = ["gtin", "mpn", "product_category", "material", "color", "condition", "length", "weight",
               "additional_image_urls", "group_id", "variant_dict", "shipping", "accepts_returns", "return_policy",
               "review_count", "q_and_a", "related_product_id", "seller_url"]
SIZE_WORDS = ("size", "sizes", "dimension", "dimensions", "format")
COLOR_WORDS = ("color", "colour", "finish", "shade")
MATERIAL_WORDS = ("material", "paper", "fabric", "composition")
# tokens kept upper-case when an ALL-CAPS title is recased (spec: avoid all caps)
ACRONYMS = {"USB", "LED", "UV", "XL", "XXL", "XS", "HD", "UHD", "LCD", "OLED", "SPF", "BPA", "USA", "UK", "EU", "DIY",
            "RGB", "GPS", "LTE", "AC", "DC", "PVC", "ABS", "EVA", "TPU", "PU", "DVD", "CD", "TV", "PC", "AI", "SUV",
            "ATV", "UTV", "RV", "OZ", "ML", "LB", "KG", "MM", "CM", "II", "III", "IV", "VR", "AR", "SUP", "BBQ", "FAQ"}
WEIGHT_UNITS = {"lbs": "lb", "lb": "lb", "kg": "kg", "kgs": "kg", "g": "g", "oz": "oz"}
DIM_UNITS = {"in": "in", "inch": "in", "inches": "in", "cm": "cm", "mm": "mm", "m": "m", "yd": "yd", "ft": "ft"}


# ----------------------------------------------------------------------------------------------------- helpers
def _txt(html, cap=5000):
    """HTML -> plain text: drops <style>/<script> blocks, tags, decodes every entity (also double-encoded tags),
    collapses whitespace so the value never carries a tab or newline into the TSV."""
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html or "")
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"&nbsp;|&#160;", " ", t)
    t = re.sub(r"&amp;", "&", t); t = re.sub(r"&quot;", '"', t); t = re.sub(r"&#8217;|&rsquo;", "'", t)
    if "&" in t:
        t = _html.unescape(t)
        t = re.sub(r"<[^>]+>", " ", t)   # content that was HTML-escaped HTML (Woo page builders do this)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:cap]


def _clean_title(title):
    """Entities decoded, whitespace collapsed, ALL-CAPS recased (keeps acronyms and tokens with digits)."""
    t = re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", str(title or "")))).strip()
    letters = re.sub(r"[^A-Za-z]", "", t)
    if len(letters) >= 4 and letters.isupper():
        words = []
        for w in t.split(" "):
            core = re.sub(r"[^A-Za-z]", "", w)
            words.append(w if (not core or core in ACRONYMS or re.search(r"\d", w)) else w.capitalize())
        return " ".join(words), True
    return t, False


def _left(deadline):
    """Seconds left in the budget (None = unlimited)."""
    return None if deadline is None else deadline - time.monotonic()


def _expired(deadline):
    left = _left(deadline)
    return left is not None and left <= 0


def _tmo(default, deadline):
    """Read timeout for one call: the default, capped by what's left of the budget (never below 3s)."""
    left = _left(deadline)
    return default if left is None else max(3.0, min(default, left))


def _get(url, timeout=15, as_json=True, meta=None):
    """GET -> parsed JSON (or text) on HTTP 200, else None. `meta` (a dict, optional) receives
    status / final_url / content_type / error / body-snippet so callers can explain a failure to a human."""
    m = meta if meta is not None else {}
    try:
        r = requests.get(url, headers=UA, timeout=(CONNECT_TIMEOUT, timeout), allow_redirects=True)
        m.update(status=r.status_code, final_url=r.url, content_type=r.headers.get("content-type", ""),
                 body=(r.text or "")[:800])
        if r.status_code != 200:
            return None
        if not as_json:
            return r.text
        try:
            return r.json()
        except ValueError:
            m["error"] = "not_json"
            return None
    except requests.Timeout:
        m["error"] = "timeout"
    except requests.ConnectionError as e:
        m["error"] = "connection"; m["detail"] = str(e)[:200]
    except Exception as e:  # pragma: no cover - anything else is still "no catalog", never a traceback
        m["error"] = type(e).__name__; m["detail"] = str(e)[:200]
    return None


def load_profile(domain):
    """Non-secret per-domain enrichment from feed_profiles.json (committed)."""
    try:
        return json.load(open(os.path.join(HERE, "feed_profiles.json"))).get(domain, {})
    except Exception:
        return {}


def shopify_meta(domain):
    """Shopify's public /meta.json: store name, currency, country, canonical domain. Falls back to /cart.js.
    Returns {"name", "currency", "country", "site"} — currency None when nothing public answered."""
    out = {"name": "", "currency": None, "country": "", "site": ""}
    m = _get(f"https://{domain}/meta.json", timeout=10)
    if isinstance(m, dict) and m.get("currency"):
        out.update(name=str(m.get("name") or "")[:70], currency=str(m["currency"]).upper(),
                   country=str(m.get("country") or "")[:2].upper(), site=str(m.get("domain") or ""))
        return out
    c = _get(f"https://{domain}/cart.js", timeout=10)
    if isinstance(c, dict) and c.get("currency"):
        out["currency"] = str(c["currency"]).upper()
    return out


def shopify_currency(domain):
    """Back-compat: (currency, detected)."""
    cur = shopify_meta(domain)["currency"]
    return (cur, True) if cur else ("USD", False)


def wp_site_name(domain):
    """WordPress site title from the REST index (public on every WP site that exposes wp-json)."""
    d = _get(f"https://{domain}/wp-json/?_fields=name,url", timeout=10)
    return _txt(str(d.get("name") or ""), cap=70) if isinstance(d, dict) else ""


# ----------------------------------------------------------------------------------------------------- policies
POLICY_PATHS = {
    "seller_privacy_policy": ("/policies/privacy-policy", "/privacy-policy/", "/privacy/", "/privacy-policy"),
    "seller_tos": ("/policies/terms-of-service", "/terms-of-service/", "/terms-and-conditions/",
                   "/terms/", "/terms-of-service", "/terms-conditions/"),
    "return_policy": ("/policies/refund-policy", "/returns-refunds/", "/returns/", "/refund-policy/", "/return-policy/"),
}
POLICY_SLUGS = {"seller_privacy_policy": ("privacy",), "seller_tos": ("terms", "conditions"), "return_policy": ("return", "refund")}


def _resolve_page(url, timeout=10):
    """Final URL of a real page (200, follows redirects — WordPress guesses permalinks), else None.
    A redirect to the homepage, or a 'not found' page served with HTTP 200 (soft 404), is not a policy page."""
    m = {}
    body = _get(url, timeout=timeout, as_json=False, meta=m)
    if body is None:
        return None
    final = m.get("final_url") or url
    if final.rstrip("/").count("/") <= 2:  # https://host or https://host/ -> homepage
        return None
    head = body[:4000].lower()
    title = re.search(r"<title[^>]*>(.*?)</title>", head, flags=re.S)
    if (title and re.search(r"not found|\b404\b|page doesn.t exist", title.group(1))) or "error404" in head:
        return None
    return final


def _policy_links_from_html(base_url, page_html, missing):
    """Footer-link fallback (any platform): first same-host <a> whose href or text names the policy."""
    found = {}
    host = urlparse(base_url).netloc.lower().replace("www.", "")
    for href, text in re.findall(r'(?is)<a\s[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', page_html or ""):
        h = href.strip()
        if not h or h.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(base_url, h)
        if urlparse(absolute).netloc.lower().replace("www.", "") != host:
            continue
        hl, tl = urlparse(absolute).path.lower(), _txt(text, cap=80).lower()
        for key in missing:
            if key not in found and any(w in hl or w in tl for w in POLICY_SLUGS[key]):
                found[key] = absolute
    return found


def discover_policies(domain, deadline=None):
    """Discover privacy + terms (checkout gate) and the returns page.
    Order of trust: well-known paths (Shopify then WordPress, probed in parallel but chosen in priority order)
    → WP pages API (slug or title) → links on the homepage (works for any platform)."""
    found = {}
    candidates = [(key, f"https://{domain}{path}") for key, paths in POLICY_PATHS.items() for path in paths]
    tmo = _tmo(10, deadline)
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(lambda ku: _resolve_page(ku[1], timeout=tmo), candidates))
    for (key, _), final in zip(candidates, results):
        if final and key not in found:
            found[key] = final
    missing = [k for k in POLICY_PATHS if k not in found]
    if missing:
        pages = _get(f"https://{domain}/wp-json/wp/v2/pages?per_page=100&_fields=slug,link,title", timeout=_tmo(15, deadline)) or []
        for key in missing:
            for pg in pages if isinstance(pages, list) else []:
                if not isinstance(pg, dict):
                    continue
                slug = (pg.get("slug") or "").lower()
                title = _txt(str((pg.get("title") or {}).get("rendered", "") if isinstance(pg.get("title"), dict) else pg.get("title") or ""), cap=80).lower()
                if any(s in slug or s in title for s in POLICY_SLUGS[key]) and pg.get("link"):
                    found[key] = pg["link"]
                    break
    missing = [k for k in POLICY_PATHS if k not in found]
    if missing:
        m = {}
        home = _get(f"https://{domain}/", timeout=_tmo(15, deadline), as_json=False, meta=m)
        if home:
            links = _policy_links_from_html(m.get("final_url") or f"https://{domain}/", home, missing)
            for key, url in links.items():
                final = _resolve_page(url, timeout=_tmo(10, deadline))
                if final:
                    found[key] = final
    return found


shopify_policies = discover_policies  # back-compat name


# ----------------------------------------------------------------------------------------------------- adapters
def pull_shopify(domain, max_pages=8, deadline=None, info=None):
    """Public catalog via /products.json (works on every standard Shopify store). Normalised in place.
    `info` (dict) receives: meta (last HTTP meta), truncated, site (canonical host after redirects)."""
    info = info if info is not None else {}
    products = []
    for page in range(1, max_pages + 1):
        m = {}
        data = _get(f"https://{domain}/products.json?limit=250&page={page}", timeout=_tmo(25, deadline), meta=m)
        info["meta"] = m
        if not isinstance(data, dict) or not isinstance(data.get("products"), list):
            break
        if page == 1:
            info["site"] = urlparse(m.get("final_url") or "").netloc or domain
            info["empty"] = not data["products"]
        products += data["products"]
        if len(data["products"]) < 250:
            break
        if page == max_pages:
            info["truncated"] = len(products)
        if _expired(deadline):
            info["truncated"] = len(products); break
    for p in products:
        if not isinstance(p, dict):
            continue
        p["_brand"] = p.get("vendor") or ""
        p["_category"] = p.get("product_type") or ""
        p["_tags"] = p.get("tags") if isinstance(p.get("tags"), list) else [t.strip() for t in (p.get("tags") or "").split(",") if t.strip()]
        for v in p.get("variants") or []:
            g = v.get("grams")
            if g:
                try:
                    v["_weight"], v["_weight_unit"] = f"{int(g) / 453.592:.2f}", "lb"
                except (TypeError, ValueError):
                    pass
            fi = v.get("featured_image")
            if isinstance(fi, dict) and fi.get("src"):
                v["_image"] = fi["src"]
            if v.get("requires_shipping") is False:
                v["_digital"] = True
    return [p for p in products if isinstance(p, dict)]


def _woo_rest_product(p):
    """WooCommerce REST v3 product (+ optional variations list) -> Shopify-ish shape."""
    variants = []
    for v in p.get("_variations") or []:
        attrs = {a.get("name"): a.get("option") for a in v.get("attributes", []) if a.get("name")}
        variants.append({
            "id": v["id"], "title": ", ".join(o for o in attrs.values() if o) or "Default Title",
            "price": v.get("price") or "0", "compare_at_price": v.get("regular_price") or None,
            "sku": v.get("sku", ""), "barcode": "", "available": v.get("stock_status") == "instock",
            "_weight": v.get("weight") or "", "_dims": v.get("dimensions") or {},
            "_image": (v.get("image") or {}).get("src"),
            **{f"option{i+1}": val for i, val in enumerate(attrs.values())},
        })
    if not variants:
        variants = [{
            "id": p["id"], "title": "Default Title", "price": p.get("price") or "0",
            "compare_at_price": p.get("regular_price") or None,
            "sku": p.get("sku", ""), "barcode": "", "available": p.get("stock_status") == "instock",
            "_weight": p.get("weight") or "", "_dims": p.get("dimensions") or {},
        }]
    opt_names = [a.get("name") for a in p.get("attributes", []) if a.get("variation") and a.get("name")]
    return {
        "id": p["id"], "title": p.get("name", ""), "handle": p.get("slug", ""),
        "body_html": p.get("description") or p.get("short_description") or "", "vendor": "", "_url": p.get("permalink"),
        "images": [{"src": i.get("src")} for i in p.get("images", []) if i.get("src")],
        "_category": " > ".join(c.get("name", "") for c in p.get("categories", [])[:1]),
        "_tags": [t.get("name", "") for t in p.get("tags") or []],
        "_rating": p.get("average_rating"), "_reviews": p.get("rating_count"),
        "_digital": bool(p.get("virtual") or p.get("downloadable")),
        "options": [{"name": n} for n in opt_names], "variants": variants,
    }


def pull_woocommerce(domain, ck, cs, max_pages=10, deadline=None):
    """WooCommerce REST (merchant-provided keys) — the fallback when the public Store API is switched off."""
    products = []
    auth = f"consumer_key={ck}&consumer_secret={cs}"
    for page in range(1, max_pages + 1):
        data = _get(f"https://{domain}/wp-json/wc/v3/products?per_page=100&page={page}&status=publish&{auth}",
                    timeout=_tmo(30, deadline))
        if not isinstance(data, list) or not data:   # an error is a dict ({"code": "woocommerce_rest_cannot_view"})
            break
        products += [p for p in data if isinstance(p, dict) and p.get("id")]
        if len(data) < 100 or _expired(deadline):
            break
    for p in products:
        if p.get("type") == "variable" and p.get("variations") and not _expired(deadline):
            vs = _get(f"https://{domain}/wp-json/wc/v3/products/{p['id']}/variations?per_page=100&{auth}",
                      timeout=_tmo(30, deadline))
            p["_variations"] = [v for v in vs if isinstance(v, dict) and v.get("id")] if isinstance(vs, list) else []
    return [_woo_rest_product(p) for p in products if p.get("type") not in ("grouped", "external")]


def _minor(amount, unit):
    """Store API prices are integer strings in minor units ('2900', unit 2) -> '29.00'."""
    try:
        return f"{int(amount) / (10 ** int(unit)):.{int(unit)}f}"
    except (TypeError, ValueError):
        return "0"


def _unit_from(formatted, table):
    """'0.48 lbs' -> 'lb'; '20 × 30 × 0.1 in' -> 'in'; '' / 'N/A' -> ''."""
    m = re.search(r"([A-Za-z]+)\s*$", str(formatted or "").strip())
    return table.get(m.group(1).lower(), "") if m else ""


def _woo_variations(domain, product, deadline):
    """All variations of one variable product in one call (Store API: ?type=variation&parent=<id>) — the same JSON
    per variation as /products/<vid> (verified byte-identical). Older Store API builds ignore the filter and answer
    with ordinary products; then we fall back to one request per variation id. Returns (list, complete)."""
    pid, wanted = product["id"], [v["id"] for v in product.get("variations") or [] if isinstance(v, dict) and v.get("id")]
    out = []
    for page in (1, 2, 3):
        if _expired(deadline):
            return out, False
        data = _get(f"https://{domain}/wp-json/wc/store/v1/products?type=variation&parent={pid}&per_page=100&page={page}",
                    timeout=_tmo(30, deadline))
        if not isinstance(data, list):
            break
        good = [d for d in data if isinstance(d, dict) and d.get("id") in wanted and (d.get("parent") == pid or d.get("type") == "variation")]
        if data and not good:
            break                                  # filter unsupported -> per-id fallback below
        out += good
        if len(data) < 100:
            return out, True
    have = {d["id"] for d in out}
    for vid in wanted:
        if vid in have:
            continue
        if _expired(deadline):
            return out, False
        d = _get(f"https://{domain}/wp-json/wc/store/v1/products/{vid}", timeout=_tmo(20, deadline))
        if isinstance(d, dict) and d.get("id"):
            out.append(d)
    return out, len(out) >= len(wanted)


def pull_woo_store(domain, max_pages=10, deadline=None, info=None):
    """WooCommerce Store API (public, no keys — ships with every Woo >= 3.6).
    Returns (products in Shopify-ish shape with _brand/_category/_attrs/_rating…, currency or None).
    `info` receives: meta (first-page HTTP meta), truncated, incomplete_variants (products whose variations
    could not be listed inside the budget — their rows fall back to parent price/stock)."""
    info = info if info is not None else {}
    products, currency = [], None
    for page in range(1, max_pages + 1):
        m = {}
        url = f"https://{domain}/wp-json/wc/store/v1/products?per_page=100&page={page}"
        data = _get(url, timeout=_tmo(30, deadline), meta=m)
        if data is None and m.get("error") == "timeout" and not _expired(deadline):
            data = _get(url, timeout=_tmo(60, deadline), meta=m)     # one slow-host retry, never a storm
        if page == 1:
            info["meta"] = m
        if not isinstance(data, list):
            if page > 1 and products:
                info["truncated"] = len(products)
            break
        products += [p for p in data if isinstance(p, dict) and p.get("id")]
        if len(data) < 100:
            break
        if page == max_pages or _expired(deadline):
            info["truncated"] = len(products); break
    products = [p for p in products if p.get("is_purchasable", True) and not p.get("is_password_protected")
                and p.get("type") not in ("grouped", "external")]
    variable = [p for p in products if p.get("variations")]
    details, incomplete = {}, 0
    if variable:
        with ThreadPoolExecutor(max_workers=6) as ex:
            for p, (lst, complete) in zip(variable, ex.map(lambda p: _woo_variations(domain, p, deadline), variable)):
                for d in lst:
                    details[d["id"]] = d
                if not complete:
                    incomplete += 1
    info["incomplete_variants"] = incomplete
    out = []
    for p in products:
        pr = p.get("prices") or {}
        unit = pr.get("currency_minor_unit", 2)
        currency = currency or pr.get("currency_code")
        opt_names = [a.get("name") for a in p.get("attributes", []) if a.get("has_variations") and a.get("name")]
        # non-variation attributes -> spec fields (material / color) + everything else kept for variant_dict context
        static_attrs = {a["name"]: ", ".join(t.get("name", "") for t in a.get("terms", []))
                        for a in p.get("attributes", []) if not a.get("has_variations") and a.get("name")}
        variants = []
        for v in p.get("variations") or []:
            if not isinstance(v, dict) or not v.get("id"):
                continue
            detail = details.get(v["id"]) or {}
            dpr = detail.get("prices") or pr
            attrs = {a.get("name"): a.get("value") for a in v.get("attributes", []) if a.get("name")}
            var = {
                "id": v["id"], "title": ", ".join(attrs.get(n, "") for n in opt_names if attrs.get(n)) or "Default Title",
                "price": _minor(dpr.get("sale_price") or dpr.get("price"), unit),
                "compare_at_price": _minor(dpr.get("regular_price"), unit) if dpr.get("regular_price") else None,
                "sku": detail.get("sku") or "", "barcode": "",
                "available": detail.get("is_in_stock", p.get("is_in_stock", True)),
                "_backorder": bool(detail.get("is_on_backorder")),
                "_weight": detail.get("weight") or "", "_dims": detail.get("dimensions") or {},
                "_weight_unit": _unit_from(detail.get("formatted_weight"), WEIGHT_UNITS),
                "_dims_unit": _unit_from(detail.get("formatted_dimensions"), DIM_UNITS),
                "_image": (detail.get("images") or [{}])[0].get("src"),
                "_desc": _txt(detail.get("description") or ""),
            }
            for i, n in enumerate(opt_names):
                var[f"option{i+1}"] = attrs.get(n)
            variants.append(var)
        if not variants:
            variants = [{
                "id": p["id"], "title": "Default Title",
                "price": _minor(pr.get("sale_price") or pr.get("price"), unit),
                "compare_at_price": _minor(pr.get("regular_price"), unit) if pr.get("regular_price") else None,
                "sku": p.get("sku") or "", "barcode": "", "available": p.get("is_in_stock", True),
                "_backorder": bool(p.get("is_on_backorder")),
                "_weight": p.get("weight") or "", "_dims": p.get("dimensions") or {},
                "_weight_unit": _unit_from(p.get("formatted_weight"), WEIGHT_UNITS),
                "_dims_unit": _unit_from(p.get("formatted_dimensions"), DIM_UNITS),
            }]
        cats = p.get("categories") or []
        out.append({
            "id": p["id"], "title": p.get("name", ""), "handle": p.get("slug", ""),
            "body_html": p.get("description") or p.get("short_description") or "",
            "vendor": "", "_url": p.get("permalink"),
            "images": [{"src": i.get("src")} for i in p.get("images") or [] if isinstance(i, dict) and i.get("src")],
            "options": [{"name": n} for n in opt_names], "variants": variants,
            "_brand": ", ".join(b.get("name", "") for b in (p.get("brands") or []))[:70],
            "_category": " > ".join(c.get("name", "") for c in cats[:1]),
            "_category_slug": (cats[0].get("slug") if cats else ""),
            "_tags": [t.get("name", "") for t in p.get("tags") or []],
            "_attrs": static_attrs,
            "_rating": p.get("average_rating"), "_reviews": p.get("review_count"),
            "_digital": False,   # Store API doesn't expose virtual/downloadable
        })
    return out, currency


# ----------------------------------------------------------------------------------------------------- diagnosis
PLATFORM_MARKERS = [
    ("BigCommerce", r"bigcommerce\.com|stencil-utils"),
    ("Magento", r"Magento_|/static/version\d+/|(?<![a-z])mage/"),
    ("Salesforce Commerce Cloud", r"demandware\.|salesforce commerce"),
    ("Squarespace", r"squarespace\.com|squarespace-cdn"),
    ("Wix", r"wix\.com|wixstatic\.com|wixsite"),
    ("PrestaShop", r"prestashop"),
    ("Shopify", r"cdn\.shopify\.com|myshopify\.com"),
    ("WooCommerce", r"woocommerce"),
    ("WordPress", r"wp-content/|wp-includes/"),
]
KEYS_HINT = "ask the merchant for read-only WooCommerce REST API keys (WooCommerce → Settings → Advanced → REST API) and we build the feed from those"


def _blocked(m):
    body = (m.get("body") or "").lower()
    ct = (m.get("content_type") or "").lower()
    status = m.get("status")
    return status in (403, 429, 503, 406) and ("html" in ct or not ct) or (status == 200 and any(
        s in body for s in ("just a moment", "cf-browser-verification", "captcha", "access denied", "attention required")))


def diagnose(domain, shop_meta, woo_meta, shop_info=None, budget=None):
    """One human-readable, actionable sentence for 'no catalog' — phrased to follow 'No public catalog found — …'."""
    shop_info = shop_info or {}
    sm, wm = shop_meta or {}, woo_meta or {}
    errs = {sm.get("error"), wm.get("error")}
    if sm.get("error") == "connection" and wm.get("error") in ("connection", None) and not wm.get("status"):
        return f"we couldn't connect to https://{domain} (DNS, SSL or the site is down) — check the domain spelling and that it is publicly reachable"
    wbody = (wm.get("body") or "").lower()
    if wm.get("status") in (401, 403) and ("rest_not_logged_in" in wbody or "rest_forbidden" in wbody or "rest_cannot" in wbody
                                           or "rest_disabled" in wbody or "application/json" in (wm.get("content_type") or "")):
        return ("this WooCommerce store's WordPress REST API is locked to logged-in users (a security plugin or a "
                f"'disable REST API' setting), so the public Store API can't be read — {KEYS_HINT}")
    if wm.get("status") == 404 and "rest_no_route" in wbody:
        return ("this is a WordPress site but the WooCommerce Store API (/wp-json/wc/store/v1) isn't there — either "
                f"WooCommerce is inactive / older than 3.6 or the shop is a different plugin; {KEYS_HINT} if it is WooCommerce")
    if _blocked(sm) or _blocked(wm):
        st = sm.get("status") if _blocked(sm) else wm.get("status")
        return (f"the store's bot protection (Cloudflare or similar, HTTP {st}) blocked our fetch — ask the merchant to "
                "allow the user agent 'CanAIShopYou feed engine' (or their firewall's verified-bot list), or provide their platform's read-only API keys")
    if sm.get("status") == 200 and (shop_info.get("empty") or "/password" in (sm.get("final_url") or "")):
        if "/password" in (sm.get("final_url") or "") or "password" in (sm.get("body") or "").lower()[:800]:
            return "this Shopify store is password-protected — ask the merchant to lift the storefront password or share a Storefront API access token"
        return ("this is a Shopify store but its public catalog is empty — no products are published to the Online Store "
                "sales channel (or the catalog lives on another domain); ask the merchant to publish products or share the shop's canonical domain")
    if sm.get("status") == 401 and not wm.get("status") in (401, 403):
        return "this Shopify store is password-protected — ask the merchant to lift the storefront password or share a Storefront API access token"
    if "timeout" in errs:
        b = f"{int(budget)}s" if budget else "our timeout"
        return f"the store took longer than {b} to answer its catalog API — retry in a few minutes, or ask the merchant for API keys so we can build it offline"
    hm = {}
    home = _get(f"https://{domain}/", timeout=12, as_json=False, meta=hm)
    if home:
        for name, pat in PLATFORM_MARKERS:
            if re.search(pat, home, flags=re.I):
                if name == "Shopify":
                    return "the site uses a Shopify theme but /products.json isn't public (headless storefront or an app blocking it) — ask the merchant for a Storefront API access token"
                if name == "WooCommerce":
                    return f"the site runs WooCommerce but the public Store API returned nothing (a plugin or firewall is blocking /wp-json) — {KEYS_HINT}"
                if name == "WordPress":
                    return "this is a WordPress site without a readable WooCommerce catalog — if it sells through WooCommerce, " + KEYS_HINT
                return f"this looks like a {name} store — {name} has no keyless public catalog API, so we connect with read-only API keys or a product CSV export instead"
    elif _blocked(hm):
        return (f"the store's bot protection (HTTP {hm.get('status')}) blocked our fetch — ask the merchant to allow the user agent "
                "'CanAIShopYou feed engine', or provide their platform's read-only API keys")
    return ("neither a Shopify (/products.json) nor a WooCommerce (/wp-json/wc/store) catalog answered on this domain — "
            "the store is on another platform or headless; we connect it with read-only API keys or a product CSV export")


# ----------------------------------------------------------------------------------------------------- rows
def _positive(val):
    """'0', '0.00', '', None are 'not provided' for weights and dimensions."""
    try:
        return float(str(val).strip()) > 0
    except (TypeError, ValueError):
        return False


def _pick(d, words):
    for k, v in (d or {}).items():
        if any(w in k.lower() for w in words) and v:
            return v
    return ""


def build_rows(domain, products, brand_name, currency, policies, profile=None, site=None):
    prof = profile or {}
    site = site or domain
    rows, issues = [], {"no_identifier": 0, "no_image": 0, "no_description": 0, "truncated_title": 0, "short_description": 0,
                        "skipped_no_image": 0, "skipped_no_price": 0, "skipped_no_title": 0, "titles_recased": 0}
    checkout_ok = bool(policies.get("seller_privacy_policy") and policies.get("seller_tos"))
    seller = (prof.get("seller_name") or brand_name or "")[:70]
    # related products: everything in the same category/set (spec: related_product_id + relationship_type)
    rel_type = prof.get("related_within_category")
    by_cat = {}
    if rel_type:
        for p in products:
            by_cat.setdefault(p.get("_category_slug") or p.get("_category") or "", []).append(str(p["id"]))
    qa = json.dumps(prof["q_and_a"], ensure_ascii=False) if prof.get("q_and_a") else ""
    ship = prof.get("shipping", "")
    for p in products:
        if not isinstance(p, dict) or p.get("id") in (None, ""):
            continue
        imgs = [i.get("src") for i in p.get("images") or [] if isinstance(i, dict) and i.get("src")]
        opt_names = [o.get("name") for o in p.get("options") or [] if o.get("name")]
        variants = [v for v in p.get("variants") or [] if isinstance(v, dict) and v.get("id") not in (None, "")]
        many = len(variants) > 1
        p_brand = (p.get("_brand") or prof.get("brand") or brand_name or "")[:70]
        cat = prof.get("product_category") or p.get("_category") or ""
        material = (_pick(p.get("_attrs"), MATERIAL_WORDS) or prof.get("material", ""))[:100]
        p_color = _pick(p.get("_attrs"), COLOR_WORDS)
        p_title, recased = _clean_title(p.get("title"))
        if not p_title:
            issues["skipped_no_title"] += len(variants); continue
        if recased:
            issues["titles_recased"] += 1
        related = ""
        if rel_type:
            others = [i for i in by_cat.get(p.get("_category_slug") or p.get("_category") or "", []) if i != str(p["id"])]
            related = ",".join(others[:20])
        p_desc = _txt(p.get("body_html"))
        for v in variants:
            vt = _txt(str(v.get("title") or ""), cap=200)
            title = p_title if vt in ("", "Default Title") else f"{p_title} - {vt}"
            if len(title) > 150:
                title = title[:150]; issues["truncated_title"] += 1
            desc = p_desc
            if v.get("_desc"):
                desc = (v["_desc"] + " " + desc)[:5000]
            if not desc:
                bits = [title] + [f"by {p_brand}" if p_brand else "", f"Category: {cat}." if cat else ""]
                desc = " ".join(b for b in bits if b).strip().rstrip(".") + "."
                issues["no_description"] += 1
            elif len(desc) < 200:
                issues["short_description"] += 1
            v_img = v.get("_image") or (imgs[0] if imgs else "")
            if not v_img:
                issues["no_image"] += 1; issues["skipped_no_image"] += 1
                continue
            price = str(v.get("price") or "").strip()
            try:
                if not re.match(r"^\d+(\.\d+)?$", price):           # '$1,299.00', '29,90' -> spec 'amount'
                    raw = price.replace(",", ".") if (price.count(",") == 1 and "." not in price) else price
                    price = f"{float(re.sub(r'[^0-9.]', '', raw)):.2f}"
                if float(price) <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                issues["skipped_no_price"] += 1
                continue
            gtin = re.sub(r"[ -]", "", str(v.get("barcode") or ""))
            gtin = gtin if gtin.isdigit() and 8 <= len(gtin) <= 14 else ""
            mpn = str(v.get("sku") or "")[:70]
            if not gtin and not mpn:
                issues["no_identifier"] += 1
            cmp_at = v.get("compare_at_price")
            on_sale = False
            try:
                on_sale = bool(cmp_at) and float(cmp_at) > float(price)
            except (TypeError, ValueError):
                pass
            vdict = {}
            for i, on in enumerate(opt_names):
                val = v.get(f"option{i+1}")
                if val and val != "Default Title":
                    vdict[on] = _txt(str(val), cap=100)
            v_size = _pick(vdict, SIZE_WORDS)
            # a real colour option on the variant wins; else the product's colour attribute; else a finish/shade option
            v_color = _pick(vdict, ("color", "colour")) or p_color or _pick(vdict, COLOR_WORDS)
            dims = {k: val for k, val in (v.get("_dims") or {}).items() if _positive(val)}
            has_dims = all(dims.get(k) for k in ("length", "width", "height"))
            if not _positive(v.get("_weight")):
                v = dict(v, _weight="")
            url = p.get("_url") or f"https://{site}/products/{p.get('handle', '')}"
            extra = [i for i in imgs if i != v_img][:10]
            avail = "backorder" if (v.get("_backorder") and not v.get("available")) else AVAIL.get(bool(v.get("available")), "unknown")
            rows.append({
                "is_eligible_search": "true",
                "is_eligible_checkout": "true" if checkout_ok else "false",
                "is_ads_eligible": prof.get("is_ads_eligible", "false"),
                "item_id": str(v["id"]), "gtin": gtin, "mpn": mpn,
                "title": title, "description": desc,
                "url": url if not many else f"{url}{'&' if '?' in url else '?'}variant={v['id']}",
                "brand": p_brand, "condition": prof.get("condition", "new"),
                "product_category": cat, "material": material,
                "length": dims.get("length", "") if has_dims else "", "width": dims.get("width", "") if has_dims else "",
                "height": dims.get("height", "") if has_dims else "",
                "dimensions_unit": (v.get("_dims_unit") or prof.get("dimensions_unit", "in")) if has_dims else "",
                "weight": v.get("_weight", ""), "item_weight_unit": (v.get("_weight_unit") or prof.get("weight_unit", "lb")) if v.get("_weight") else "",
                "age_group": prof.get("age_group", ""),
                "image_url": v_img, "additional_image_urls": ",".join(extra), "video_url": prof.get("video_url", ""),
                "price": f"{cmp_at} {currency}" if on_sale else f"{price} {currency}",
                "sale_price": f"{price} {currency}" if on_sale else "",
                "availability": avail, "availability_date": "",
                "group_id": str(p["id"]) if many else "",
                "listing_has_variations": "true" if many else "false",
                "variant_dict": json.dumps(vdict, ensure_ascii=False) if vdict else "",
                "item_group_title": p_title[:150] if many else "",
                "color": (v_color or "")[:40], "size": (v_size or "")[:20],
                "shipping": ship, "is_digital": "true" if (p.get("_digital") or v.get("_digital")) else "false",
                "seller_name": seller, "seller_url": prof.get("seller_url") or f"https://{site}",
                "seller_privacy_policy": policies.get("seller_privacy_policy", ""),
                "seller_tos": policies.get("seller_tos", ""),
                "accepts_returns": prof.get("accepts_returns", ""), "return_deadline_in_days": prof.get("return_deadline_in_days", ""),
                "accepts_exchanges": prof.get("accepts_exchanges", ""),
                "return_policy": prof.get("return_policy") or policies.get("return_policy", ""),
                "popularity_score": prof.get("popularity_score", ""),
                "review_count": str(p["_reviews"]) if p.get("_reviews") else "",
                "star_rating": str(p["_rating"]) if p.get("_reviews") and p.get("_rating") not in (None, "0", 0) else "",
                "q_and_a": qa, "related_product_id": related, "relationship_type": rel_type if related else "",
                "target_countries": prof.get("target_countries", "US"), "store_country": prof.get("store_country", ""),
            })
    return rows, issues, checkout_ok


def validate(rows, check_urls=0):
    """Spec conformance + completeness. Returns {errors, warnings, completeness, recommended_missing}."""
    errors, warnings = [], []
    filled = {f: 0 for f in RECOMMENDED}
    seen = set()
    for i, r in enumerate(rows):
        for f in REQUIRED:
            if not r.get(f):
                errors.append(f"row {i} ({r.get('item_id')}): required field '{f}' empty")
        if r["item_id"] in seen:
            errors.append(f"row {i}: duplicate item_id {r['item_id']}")
        seen.add(r["item_id"])
        if len(r["title"]) > 150: errors.append(f"row {i}: title > 150 chars")
        if r["title"].isupper(): warnings.append(f"row {i}: title is all caps")
        if len(r["description"]) > 5000: errors.append(f"row {i}: description > 5000 chars")
        if re.search(r"<[a-zA-Z/][^<>]*>", r["description"]): errors.append(f"row {i}: description contains HTML")
        if not re.match(r"^\d+(\.\d+)? [A-Z]{3}$", r["price"]):
            errors.append(f"row {i}: price '{r['price']}' must be 'amount CUR'")
        else:
            if float(r["price"].split()[0]) <= 0: errors.append(f"row {i}: price must be above 0")
            try:
                if r["sale_price"] and float(r["sale_price"].split()[0]) > float(r["price"].split()[0]):
                    errors.append(f"row {i}: sale_price above price")
            except (ValueError, IndexError):
                errors.append(f"row {i}: sale_price '{r['sale_price']}' must be 'amount CUR'")
        if r["availability"] not in ("in_stock", "out_of_stock", "pre_order", "backorder", "unknown"):
            errors.append(f"row {i}: bad availability {r['availability']}")
        for uf in ("url", "image_url", "seller_privacy_policy", "seller_tos", "return_policy"):
            if r.get(uf) and not r[uf].startswith("https://"):
                warnings.append(f"row {i}: {uf} not https")
        if r["is_eligible_checkout"] == "true" and not (r["seller_privacy_policy"] and r["seller_tos"]):
            errors.append(f"row {i}: checkout eligible without privacy/tos")
        if any(r.get(k) for k in ("length", "width", "height")) and not r.get("dimensions_unit"):
            errors.append(f"row {i}: dimensions without dimensions_unit")
        if r.get("weight") and not r.get("item_weight_unit"):
            errors.append(f"row {i}: weight without item_weight_unit")
        if r.get("gtin") and not (r["gtin"].isdigit() and 8 <= len(r["gtin"]) <= 14): errors.append(f"row {i}: bad gtin")
        for f in RECOMMENDED:
            if r.get(f): filled[f] += 1
    n = max(len(rows), 1)
    completeness = {f: round(100 * c / n) for f, c in filled.items()}
    score = round(sum(completeness.values()) / len(RECOMMENDED))
    if check_urls:
        import random
        for r in random.sample(rows, min(check_urls, len(rows))):
            for uf in ("url", "image_url"):
                try:
                    code = requests.head(r[uf], headers=UA, timeout=10, allow_redirects=True).status_code
                    if code != 200: errors.append(f"{uf} {r[uf]} -> HTTP {code}")
                except Exception as e:
                    errors.append(f"{uf} {r[uf]} unreachable ({type(e).__name__})")
    return {"errors": errors[:50], "error_count": len(errors), "warnings": warnings[:50], "warning_count": len(warnings),
            "recommended_completeness_pct": score, "per_field_pct": completeness,
            "recommended_missing": [f for f, c in completeness.items() if c == 0]}


def write_feed(domain, rows, outdir="feeds"):
    """Stable filenames (OpenAI: overwrite the same name each snapshot). TSV + TSV.GZ + CSV.GZ."""
    os.makedirs(outdir, exist_ok=True)
    tsv_path = os.path.join(outdir, f"{domain}.tsv")
    with open(tsv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, delimiter="\t", extrasaction="ignore", quoting=csv.QUOTE_MINIMAL)
        w.writeheader(); w.writerows(rows)
    tsv_gz = os.path.join(outdir, f"{domain}.tsv.gz")
    with open(tsv_path, "rb") as src, gzip.open(tsv_gz, "wb") as dst:
        dst.write(src.read())
    gz_path = os.path.join(outdir, f"{domain}.csv.gz")
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS, extrasaction="ignore")
    w.writeheader(); w.writerows(rows)
    with gzip.open(gz_path, "wt", encoding="utf-8") as f:
        f.write(buf.getvalue())
    return tsv_path, gz_path


GOOGLE_FIELDS = ["id", "item_group_id", "title", "description", "link", "image_link", "additional_image_link",
                 "availability", "price", "sale_price", "brand", "gtin", "mpn", "identifier_exists", "condition",
                 "product_type", "google_product_category", "color", "size", "material", "age_group",
                 "shipping", "shipping_weight", "product_length", "product_width", "product_height",
                 "product_detail", "custom_label_0"]


def to_google_rows(rows):
    """Same catalog in Google Merchant Center column names — accepted as-is by Google Merchant Center (scheduled
    fetch), Microsoft Merchant Center, Meta Commerce Manager and Perplexity's merchant program. Those four are
    the self-serve AI shopping surfaces (Gemini/AI Mode, Copilot, Meta AI, Perplexity)."""
    out = []
    for r in rows:
        ship = ""
        if r.get("shipping"):
            p = r["shipping"].split(":")           # country:region:service:price:hmin:hmax:tmin:tmax
            ship = ":".join(p[:4]) if len(p) >= 4 else r["shipping"]
        out.append({
            "id": r["item_id"], "item_group_id": r.get("group_id", ""),
            "title": r["title"], "description": r["description"], "link": r["url"],
            "image_link": r["image_url"], "additional_image_link": r.get("additional_image_urls", ""),
            "availability": r["availability"].replace("_", " "),   # in stock / out of stock / preorder / backorder
            "price": r["price"], "sale_price": r.get("sale_price", ""),
            "brand": r["brand"], "gtin": r.get("gtin", ""), "mpn": r.get("mpn", ""),
            "identifier_exists": "yes" if (r.get("gtin") or r.get("mpn")) else "no",
            "condition": r.get("condition") or "new",
            "product_type": r.get("product_category", ""), "google_product_category": r.get("product_category", ""),
            "color": r.get("color", ""), "size": r.get("size", ""), "material": r.get("material", ""),
            "age_group": r.get("age_group", ""), "shipping": ship,
            "shipping_weight": f"{r['weight']} {r['item_weight_unit']}" if r.get("weight") else "",
            "product_length": f"{r['length']} {r['dimensions_unit']}" if r.get("length") else "",
            "product_width": f"{r['width']} {r['dimensions_unit']}" if r.get("width") else "",
            "product_height": f"{r['height']} {r['dimensions_unit']}" if r.get("height") else "",
            "product_detail": "Series:Series 01" if "Series 01" in (r.get("description") or "") else "",
            "custom_label_0": r.get("seller_name", ""),
        })
    return out


def write_google_feed(domain, rows, outdir="feeds"):
    path = os.path.join(outdir, f"{domain}.google.tsv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=GOOGLE_FIELDS, delimiter="\t", extrasaction="ignore", quoting=csv.QUOTE_MINIMAL)
        w.writeheader(); w.writerows(to_google_rows(rows))
    return path


SHOPIFY_FIELDS = ["Handle", "Title", "Body (HTML)", "Vendor", "Product Category", "Type", "Tags", "Published",
                  "Option1 Name", "Option1 Value", "Option2 Name", "Option2 Value", "Option3 Name", "Option3 Value",
                  "Variant SKU", "Variant Grams", "Variant Inventory Policy", "Variant Fulfillment Service",
                  "Variant Price", "Variant Compare At Price", "Variant Requires Shipping", "Variant Taxable",
                  "Variant Barcode", "Image Src", "Image Position", "Image Alt Text", "SEO Title", "SEO Description",
                  "Variant Weight Unit", "Status"]


def write_shopify_csv(domain, rows, outdir="feeds"):
    """Shopify product-import CSV built from the spec rows — for the free Shopify Agentic plan, which puts a
    non-Shopify store's catalog into ChatGPT via Shopify Catalog (checkout stays on the merchant's own site).
    Grouped by group_id; first row of each product carries body/vendor/images, later rows only variant columns."""
    groups = {}
    for r in rows:
        groups.setdefault(r.get("group_id") or r["item_id"], []).append(r)
    out = []
    for gid, vs in groups.items():
        first = vs[0]
        handle = re.sub(r"[^a-z0-9]+", "-", (first.get("item_group_title") or first["title"]).lower()).strip("-")[:100]
        body = "<p>" + first["description"].replace("\n", " ") + "</p>"
        imgs = [first["image_url"]] + [u for u in (first.get("additional_image_urls") or "").split(",") if u]
        opt_names = []
        try:
            opt_names = list(json.loads(first["variant_dict"]).keys()) if first.get("variant_dict") else []
        except Exception:
            pass
        for i, r in enumerate(vs):
            vd = {}
            try:
                vd = json.loads(r["variant_dict"]) if r.get("variant_dict") else {}
            except Exception:
                pass
            grams = ""
            if r.get("weight"):
                try:
                    grams = str(int(round(float(r["weight"]) * (453.592 if r.get("item_weight_unit", "lb").startswith("lb") else 1000))))
                except ValueError:
                    pass
            price = r["price"].split()[0]; sale = r.get("sale_price", "").split()[0] if r.get("sale_price") else ""
            row = {
                "Handle": handle, "Title": (first.get("item_group_title") or first["title"]) if i == 0 else "",
                "Body (HTML)": body if i == 0 else "", "Vendor": first["brand"] if i == 0 else "",
                "Product Category": first.get("product_category", "") if i == 0 else "",
                "Type": (first.get("product_category", "").split(">")[-1].strip()) if i == 0 else "",
                "Tags": "" if i else ", ".join(t for t in (first.get("material", ""), first.get("color", "")) if t),
                "Published": "TRUE" if i == 0 else "",
                "Option1 Name": (opt_names[0] if opt_names else "Title") if i == 0 else "",
                "Option1 Value": vd.get(opt_names[0], "Default Title") if opt_names else "Default Title",
                "Option2 Name": (opt_names[1] if len(opt_names) > 1 else "") if i == 0 else "",
                "Option2 Value": vd.get(opt_names[1], "") if len(opt_names) > 1 else "",
                "Option3 Name": (opt_names[2] if len(opt_names) > 2 else "") if i == 0 else "",
                "Option3 Value": vd.get(opt_names[2], "") if len(opt_names) > 2 else "",
                "Variant SKU": r.get("mpn", ""), "Variant Grams": grams,
                "Variant Inventory Policy": "continue", "Variant Fulfillment Service": "manual",
                "Variant Price": sale or price, "Variant Compare At Price": price if sale else "",
                "Variant Requires Shipping": "FALSE" if r.get("is_digital") == "true" else "TRUE", "Variant Taxable": "TRUE",
                "Variant Barcode": r.get("gtin", ""),
                "Image Src": imgs[i] if i < len(imgs) else "", "Image Position": str(i + 1) if i < len(imgs) else "",
                "Image Alt Text": (first.get("item_group_title") or first["title"]) if i < len(imgs) else "",
                "SEO Title": first["title"][:70] if i == 0 else "", "SEO Description": first["description"][:320] if i == 0 else "",
                "Variant Weight Unit": "lb" if r.get("item_weight_unit", "").startswith("lb") else ("kg" if r.get("weight") else ""),
                "Status": "active" if i == 0 else "",
            }
            out.append(row)
        # remaining images on extra rows (Shopify convention)
        for j in range(len(vs), len(imgs)):
            out.append({"Handle": handle, "Image Src": imgs[j], "Image Position": str(j + 1),
                        "Image Alt Text": first.get("item_group_title") or first["title"]})
    path = os.path.join(outdir, f"{domain}.shopify.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SHOPIFY_FIELDS, extrasaction="ignore")
        w.writeheader(); w.writerows(out)
    return path


def push_sftp(local_path, host, username, remote_dir="/", password=None, key_path=None, port=22):
    """Upload a snapshot to the SFTP location OpenAI assigns at approval (same filename every time)."""
    import paramiko
    t = paramiko.Transport((host, port))
    if key_path:
        t.connect(username=username, pkey=paramiko.RSAKey.from_private_key_file(key_path))
    else:
        t.connect(username=username, password=password)
    sftp = paramiko.SFTPClient.from_transport(t)
    remote = remote_dir.rstrip("/") + "/" + os.path.basename(local_path)
    sftp.put(local_path, remote + ".tmp"); sftp.posix_rename(remote + ".tmp", remote)
    sftp.close(); t.close()
    return remote


# ----------------------------------------------------------------------------------------------------- run
def normalize_domain(domain):
    d = re.sub(r"^\s*https?://", "", str(domain or "").strip().lower()).split("/")[0].split("?")[0].split("#")[0]
    return d.split("@")[-1].split(":")[0].strip(".")


def run(domain, brand_name=None, woo_keys=None, outdir="feeds", check_urls=0, budget=None):
    """Build every feed for one store. NEVER raises: a store-side problem (and even an internal bug) comes back as
    {"ok": False, "reason": <one actionable sentence>} so /connect always shows a human explanation.
    `budget` = seconds for the whole build (None -> FEED_TIME_BUDGET env, default 90; 0 -> unlimited)."""
    try:
        return _run(domain, brand_name, woo_keys, outdir, check_urls, budget)
    except Exception as e:
        import traceback
        traceback.print_exc(file=sys.stderr)   # -> Render logs
        return {"ok": False, "domain": normalize_domain(domain),
                "reason": f"an unexpected error hit while reading this store ({type(e).__name__}) — it's logged; "
                          "email us the domain and we'll build the feed by hand"}


def _run(domain, brand_name, woo_keys, outdir, check_urls, budget):
    domain = normalize_domain(domain)
    if not re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", domain):
        return {"ok": False, "domain": domain, "reason": f"'{domain}' isn't a store domain — enter it like store.com"}
    budget = DEFAULT_BUDGET if budget is None else float(budget)
    deadline = (time.monotonic() + budget) if budget > 0 else None
    t0 = time.monotonic()
    profile = load_profile(domain)
    has_profile = bool(profile)
    notes = []
    site = domain
    shop_info, woo_info = {}, {}
    products, platform = pull_shopify(domain, deadline=deadline, info=shop_info), "shopify"
    currency, cur_detected, store_country, site_name = "USD", False, "", ""
    if products:
        meta = shopify_meta(domain)
        if meta["currency"]:
            currency, cur_detected = meta["currency"], True
        store_country, site_name = meta["country"], meta["name"]
        site = shop_info.get("site") or domain          # canonical host after redirects (product URLs)
    else:
        products, cur = pull_woo_store(domain, deadline=deadline, info=woo_info)  # public Store API — no keys needed
        platform = "woocommerce"
        if cur:
            currency, cur_detected = cur, True
        if not products and woo_keys:
            products = pull_woocommerce(domain, *woo_keys, deadline=deadline)
            platform = "woocommerce_rest"
        if products:
            site_name = wp_site_name(domain)
    if not products:
        reason = diagnose(domain, shop_info.get("meta"), woo_info.get("meta"), shop_info, budget)
        return {"ok": False, "domain": domain, "reason": reason,
                "diagnosis": {"shopify": {k: v for k, v in (shop_info.get("meta") or {}).items() if k != "body"},
                              "woocommerce": {k: v for k, v in (woo_info.get("meta") or {}).items() if k != "body"}},
                "seconds": round(time.monotonic() - t0, 1)}
    policies = discover_policies(domain, deadline=deadline)
    # brand: explicit arg > profile > a short site title ("Nalgene", not "Nalgene | Bottles - Shop") > the domain word
    site_brand = site_name if (site_name and 2 <= len(site_name) <= 40 and not re.search(r"[|–—]| - ", site_name)) else ""
    brand_name = brand_name or profile.get("brand") or site_brand or domain.split(".")[0].replace("-", " ").title()
    if not profile.get("seller_name") and site_name:
        profile = dict(profile, seller_name=site_name)
    if not profile.get("store_country") and store_country:
        profile = dict(profile, store_country=store_country)
    rows, issues, checkout_ok = build_rows(domain, products, brand_name, currency, policies, profile, site=site)
    if not rows:
        return {"ok": False, "domain": domain, "platform": platform, "products": len(products),
                "reason": (f"{len(products)} products were read but none can go in a feed — every variant lacks an image "
                           f"or a price ({issues['skipped_no_image']} without image, {issues['skipped_no_price']} without price)"),
                "data_quality": issues, "seconds": round(time.monotonic() - t0, 1)}
    tsv, gz = write_feed(domain, rows, outdir)
    gfeed = write_google_feed(domain, rows, outdir)
    sfeed = write_shopify_csv(domain, rows, outdir)
    v = validate(rows, check_urls=check_urls)
    skipped = issues["skipped_no_image"] + issues["skipped_no_price"] + issues["skipped_no_title"]
    if skipped:
        notes.append(f"{skipped} variant rows skipped (cannot pass the spec): {issues['skipped_no_image']} without an image, "
                     f"{issues['skipped_no_price']} without a price, {issues['skipped_no_title']} without a title")
    trunc = shop_info.get("truncated") or woo_info.get("truncated")
    if trunc:
        notes.append(f"catalog truncated at {trunc} products (store cap or time budget) — the full build runs offline with no budget")
    if woo_info.get("incomplete_variants"):
        notes.append(f"variant details for {woo_info['incomplete_variants']} products could not be listed within the time budget — "
                     "those rows use the parent product's price/stock; rebuild offline for exact variant data")
    if not cur_detected:
        notes.append("currency not published by the store — USD assumed; confirm with the merchant")
    if issues["titles_recased"]:
        notes.append(f"{issues['titles_recased']} ALL-CAPS titles recased (spec asks for sentence/title case)")
    report = {
        "ok": True, "domain": domain, "platform": platform, "site_name": site_name,
        "products": len(products), "feed_rows": len(rows),
        "files": {"tsv": tsv, "tsv_gz": tsv[:-4] + ".tsv.gz", "csv_gz": gz, "google_tsv": gfeed, "shopify_csv": sfeed},
        "currency": currency, "currency_detected": cur_detected,
        "search_eligible": True,
        "checkout_eligible": checkout_ok,
        "checkout_blockers": [] if checkout_ok else
            [k for k in ("seller_privacy_policy", "seller_tos") if not policies.get(k)],
        "policies": policies,
        "data_quality": issues,
        "spec": v,
        "profile_applied": has_profile,
        "notes": notes,
        "seconds": round(time.monotonic() - t0, 1),
    }
    with open(os.path.join(outdir, f"{domain}.report.json"), "w") as f:
        json.dump(report, f, indent=2)
    return report


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python3 feed_engine.py <domain> [--budget SECONDS] [--check-urls N]"); sys.exit(1)
    n = int(sys.argv[sys.argv.index("--check-urls") + 1]) if "--check-urls" in sys.argv else 0
    b = float(sys.argv[sys.argv.index("--budget") + 1]) if "--budget" in sys.argv else 0   # CLI: unlimited by default
    print(json.dumps(run(sys.argv[1], check_urls=n, budget=b), indent=2))
