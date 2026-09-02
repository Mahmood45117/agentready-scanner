"""
feed_engine.py — the core of the ACP gateway: store catalog in → OpenAI-spec product feed out.

Implements the OpenAI product feed specification (developers.openai.com/commerce):
  - required fields: item_id, title, description, url, image_url, brand, price, availability
  - control flags:   is_eligible_search, is_eligible_checkout, is_ads_eligible
  - checkout gate:   seller_privacy_policy + seller_tos URLs must resolve
  - formats:         .tsv + .csv.gz (UTF-8, lowercase underscore headers, one variant per row)

Adapters: Shopify (public /products.json), WooCommerce Store API (public, keyless — every Woo >= 3.6),
          WooCommerce REST (merchant keys, fallback for stores that disable the Store API).
CLI:  python3 feed_engine.py <domain>   -> writes feeds/<domain>.tsv + .csv.gz + report JSON
"""
import csv, gzip, io, json, re, sys, os
import requests

UA = {"User-Agent": "Mozilla/5.0 (CanAIShopYou feed engine)"}
AVAIL = {True: "in_stock", False: "out_of_stock"}

FIELDS = ["item_id", "title", "description", "url", "image_url", "additional_image_urls",
          "brand", "price", "sale_price", "availability", "gtin", "mpn", "identifier_exists",
          "group_id", "listing_has_variations", "variant_dict",
          "seller_name", "seller_url", "seller_privacy_policy", "seller_tos",
          "is_eligible_search", "is_eligible_checkout", "is_ads_eligible"]


def _txt(html, cap=5000):
    t = re.sub(r"<[^>]+>", " ", html or "")
    t = re.sub(r"\s+", " ", t).strip()
    return t[:cap]


def _get(url, timeout=15, as_json=True):
    try:
        r = requests.get(url, headers=UA, timeout=timeout, allow_redirects=True)
        if r.status_code != 200:
            return None
        return r.json() if as_json else r.text
    except Exception:
        return None  # unreachable / not JSON — caller treats as "no catalog"


def shopify_currency(domain):
    try:
        c = _get(f"https://{domain}/cart.js")
        if c and c.get("currency"):
            return c["currency"], True
    except Exception:
        pass
    return "USD", False  # assumed — flagged in report


POLICY_PATHS = {
    "seller_privacy_policy": ("/policies/privacy-policy", "/privacy-policy/", "/privacy/", "/privacy-policy"),
    "seller_tos": ("/policies/terms-of-service", "/terms-of-service/", "/terms-and-conditions/",
                   "/terms/", "/terms-of-service", "/terms-conditions/"),
}
POLICY_SLUGS = {"seller_privacy_policy": ("privacy",), "seller_tos": ("terms", "conditions")}


def _resolve_page(url):
    """Final URL of a real page (200, follows redirects — WordPress guesses permalinks), else None.
    A redirect to the homepage is not a policy page."""
    try:
        r = requests.get(url, headers=UA, timeout=10, allow_redirects=True)
        if r.status_code != 200:
            return None
        final = r.url
        if final.rstrip("/").count("/") <= 2:  # https://host or https://host/ -> homepage
            return None
        return final
    except Exception:
        return None


def discover_policies(domain):
    """Discover the two URLs that gate checkout eligibility (Shopify paths, then Woo/WP paths, then WP pages API)."""
    found = {}
    for key, paths in POLICY_PATHS.items():
        for path in paths:
            final = _resolve_page(f"https://{domain}{path}")
            if final:
                found[key] = final
                break
    missing = [k for k in POLICY_PATHS if k not in found]
    if missing:
        pages = _get(f"https://{domain}/wp-json/wp/v2/pages?per_page=100&_fields=slug,link") or []
        for key in missing:
            for pg in pages if isinstance(pages, list) else []:
                slug = (pg.get("slug") or "").lower()
                if any(s in slug for s in POLICY_SLUGS[key]) and pg.get("link"):
                    found[key] = pg["link"]
                    break
    return found


shopify_policies = discover_policies  # back-compat name


def pull_shopify(domain, max_pages=4):
    """Public catalog via /products.json (works on every standard Shopify store)."""
    products = []
    for page in range(1, max_pages + 1):
        data = _get(f"https://{domain}/products.json?limit=250&page={page}")
        if not data or not data.get("products"):
            break
        products += data["products"]
        if len(data["products"]) < 250:
            break
    return products


def pull_woocommerce(domain, ck, cs, max_pages=10):
    """WooCommerce REST (merchant-provided keys) — the non-Shopify onboarding path."""
    products = []
    for page in range(1, max_pages + 1):
        data = _get(f"https://{domain}/wp-json/wc/v3/products?per_page=100&page={page}"
                    f"&consumer_key={ck}&consumer_secret={cs}")
        if not data:
            break
        products += data
        if len(data) < 100:
            break
    # normalize Woo -> Shopify-ish shape so one transformer serves both
    out = []
    for p in products:
        out.append({
            "id": p["id"], "title": p.get("name", ""), "handle": p.get("slug", ""),
            "body_html": p.get("description", ""), "vendor": "", "_url": p.get("permalink"),
            "images": [{"src": i.get("src")} for i in p.get("images", [])],
            "options": [], "variants": [{
                "id": p["id"], "title": "Default Title", "price": p.get("price") or "0",
                "compare_at_price": p.get("regular_price") or None,
                "sku": p.get("sku", ""), "barcode": "", "available": p.get("stock_status") == "instock",
            }],
        })
    return out


def _minor(amount, unit):
    """Store API prices are integer strings in minor units ('2900', unit 2) -> '29.00'."""
    try:
        return f"{int(amount) / (10 ** int(unit)):.{int(unit)}f}"
    except (TypeError, ValueError):
        return "0"


def pull_woo_store(domain, max_pages=10, max_variations=400):
    """WooCommerce Store API (public, no keys — ships with every Woo >= 3.6).
    Returns (products in Shopify-ish shape, currency or None)."""
    products, currency = [], None
    for page in range(1, max_pages + 1):
        data = _get(f"https://{domain}/wp-json/wc/store/v1/products?per_page=100&page={page}")
        if not data or not isinstance(data, list):
            break
        products += data
        if len(data) < 100:
            break
    products = [p for p in products if p.get("is_purchasable", True) and not p.get("is_password_protected")]
    # per-variation price/SKU/stock needs one call each — fetch them concurrently (a cold Render
    # instance must answer the crawler well inside its timeout)
    var_ids = [v["id"] for p in products for v in (p.get("variations") or [])][:max_variations]
    details = {}
    if var_ids:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as ex:
            for vid, det in zip(var_ids, ex.map(lambda i: _get(f"https://{domain}/wp-json/wc/store/v1/products/{i}"), var_ids)):
                details[vid] = det
    out = []
    for p in products:
        pr = p.get("prices") or {}
        unit = pr.get("currency_minor_unit", 2)
        currency = currency or pr.get("currency_code")
        opt_names = [a.get("name") for a in p.get("attributes", []) if a.get("has_variations") and a.get("name")]
        variants = []
        for v in p.get("variations") or []:
            detail = details.get(v["id"])
            dpr = (detail or {}).get("prices") or pr
            attrs = {a.get("name"): a.get("value") for a in v.get("attributes", []) if a.get("name")}
            var = {
                "id": v["id"], "title": ", ".join(attrs.get(n, "") for n in opt_names if attrs.get(n)) or "Default Title",
                "price": _minor(dpr.get("sale_price") or dpr.get("price"), unit),
                "compare_at_price": _minor(dpr.get("regular_price"), unit) if dpr.get("regular_price") else None,
                "sku": (detail or {}).get("sku") or "", "barcode": "",
                "available": (detail or {}).get("is_in_stock", p.get("is_in_stock", True)),
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
            }]
        out.append({
            "id": p["id"], "title": p.get("name", ""), "handle": p.get("slug", ""),
            "body_html": p.get("description") or p.get("short_description") or "",
            "vendor": "", "_url": p.get("permalink"),
            "images": [{"src": i.get("src")} for i in p.get("images", []) if i.get("src")],
            "options": [{"name": n} for n in opt_names], "variants": variants,
        })
    return out, currency


def build_rows(domain, products, brand_name, currency, policies):
    rows, issues = [], {"no_identifier": 0, "no_image": 0, "no_description": 0, "truncated_title": 0}
    checkout_ok = bool(policies.get("seller_privacy_policy") and policies.get("seller_tos"))
    for p in products:
        imgs = [i.get("src") for i in p.get("images", []) if i.get("src")]
        opt_names = [o.get("name") for o in p.get("options", []) if o.get("name")]
        many = len(p.get("variants", [])) > 1
        for v in p["variants"]:
            vt = v.get("title") or ""
            title = p["title"] if vt in ("", "Default Title") else f"{p['title']} - {vt}"
            if len(title) > 150:
                title = title[:150]; issues["truncated_title"] += 1
            desc = _txt(p.get("body_html"))
            if not desc:
                desc = title; issues["no_description"] += 1
            if not imgs:
                issues["no_image"] += 1
            gtin = re.sub(r"[ -]", "", str(v.get("barcode") or ""))
            gtin = gtin if gtin.isdigit() and 8 <= len(gtin) <= 14 else ""
            mpn = (v.get("sku") or "")[:70]
            if not gtin and not mpn:
                issues["no_identifier"] += 1
            price = v.get("price") or "0"
            cmp_at = v.get("compare_at_price")
            on_sale = False
            try:
                on_sale = cmp_at and float(cmp_at) > float(price)
            except (TypeError, ValueError):
                pass
            vdict = {}
            for i, on in enumerate(opt_names):
                val = v.get(f"option{i+1}")
                if val and val != "Default Title":
                    vdict[on] = val
            url = p.get("_url") or f"https://{domain}/products/{p['handle']}"
            rows.append({
                "item_id": str(v["id"]),
                "title": title,
                "description": desc,
                "url": url if not many else f"{url}?variant={v['id']}",
                "image_url": imgs[0] if imgs else "",
                "additional_image_urls": ",".join(imgs[1:11]),
                "brand": (brand_name or "")[:70],
                "price": f"{cmp_at} {currency}" if on_sale else f"{price} {currency}",
                "sale_price": f"{price} {currency}" if on_sale else "",
                "availability": AVAIL.get(bool(v.get("available")), "unknown"),
                "gtin": gtin, "mpn": mpn,
                "identifier_exists": "no" if (not gtin and not mpn) else "",
                "group_id": str(p["id"]) if many else "",
                "listing_has_variations": "true" if many else "false",
                "variant_dict": json.dumps(vdict) if vdict else "",
                "seller_name": (brand_name or "")[:70],
                "seller_url": f"https://{domain}",
                "seller_privacy_policy": policies.get("seller_privacy_policy", ""),
                "seller_tos": policies.get("seller_tos", ""),
                "is_eligible_search": "true",
                "is_eligible_checkout": "true" if checkout_ok else "false",
                "is_ads_eligible": "false",
            })
    return rows, issues, checkout_ok


def write_feed(domain, rows, outdir="feeds"):
    os.makedirs(outdir, exist_ok=True)
    tsv_path = os.path.join(outdir, f"{domain}.tsv")
    with open(tsv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, delimiter="\t",
                           extrasaction="ignore", quoting=csv.QUOTE_MINIMAL)
        w.writeheader(); w.writerows(rows)
    gz_path = os.path.join(outdir, f"{domain}.csv.gz")
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS, extrasaction="ignore")
    w.writeheader(); w.writerows(rows)
    with gzip.open(gz_path, "wt", encoding="utf-8") as f:
        f.write(buf.getvalue())
    return tsv_path, gz_path


def run(domain, brand_name=None, woo_keys=None, outdir="feeds"):
    domain = re.sub(r"^https?://", "", domain.strip().lower()).split("/")[0]
    brand_name = brand_name or domain.split(".")[0].replace("-", " ").title()
    products, platform = pull_shopify(domain), "shopify"
    currency, cur_detected = ("USD", False)
    if products:
        currency, cur_detected = shopify_currency(domain)
    else:
        products, cur = pull_woo_store(domain)  # public Store API — no keys needed
        platform = "woocommerce"
        if cur:
            currency, cur_detected = cur, True
        if not products and woo_keys:
            products = pull_woocommerce(domain, *woo_keys)
    if not products:
        return {"ok": False, "domain": domain,
                "reason": "no public catalog found (not Shopify or WooCommerce? connect API keys or upload CSV)"}
    policies = discover_policies(domain)
    rows, issues, checkout_ok = build_rows(domain, products, brand_name, currency, policies)
    tsv, gz = write_feed(domain, rows, outdir)
    report = {
        "ok": True, "domain": domain, "platform": platform,
        "products": len(products), "feed_rows": len(rows),
        "files": {"tsv": tsv, "csv_gz": gz},
        "currency": currency, "currency_detected": cur_detected,
        "search_eligible": True,
        "checkout_eligible": checkout_ok,
        "checkout_blockers": [] if checkout_ok else
            [k for k in ("seller_privacy_policy", "seller_tos") if not policies.get(k)],
        "data_quality": issues,
    }
    with open(os.path.join(outdir, f"{domain}.report.json"), "w") as f:
        json.dump(report, f, indent=2)
    return report


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python3 feed_engine.py <domain>"); sys.exit(1)
    print(json.dumps(run(sys.argv[1]), indent=2))
