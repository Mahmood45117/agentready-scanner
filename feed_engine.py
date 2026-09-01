"""
feed_engine.py — the core of the ACP gateway: store catalog in → OpenAI-spec product feed out.

Implements the OpenAI product feed specification (developers.openai.com/commerce):
  - required fields: item_id, title, description, url, image_url, brand, price, availability
  - control flags:   is_eligible_search, is_eligible_checkout, is_ads_eligible
  - checkout gate:   seller_privacy_policy + seller_tos URLs must resolve
  - formats:         .tsv + .csv.gz (UTF-8, lowercase underscore headers, one variant per row)

Adapters: Shopify (public /products.json — zero-friction onboarding), WooCommerce (REST keys).
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
    r = requests.get(url, headers=UA, timeout=timeout, allow_redirects=True)
    if r.status_code != 200:
        return None
    return r.json() if as_json else r.text


def shopify_currency(domain):
    try:
        c = _get(f"https://{domain}/cart.js")
        if c and c.get("currency"):
            return c["currency"], True
    except Exception:
        pass
    return "USD", False  # assumed — flagged in report


def shopify_policies(domain):
    """Discover the two URLs that gate checkout eligibility."""
    found = {}
    for key, path in (("seller_privacy_policy", "/policies/privacy-policy"),
                      ("seller_tos", "/policies/terms-of-service")):
        try:
            r = requests.get(f"https://{domain}{path}", headers=UA, timeout=10, allow_redirects=True)
            if r.status_code == 200:
                found[key] = f"https://{domain}{path}"
        except Exception:
            pass
    return found


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
    if woo_keys:
        products = pull_woocommerce(domain, *woo_keys)
        currency, cur_detected = "USD", False
        platform = "woocommerce"
    else:
        products = pull_shopify(domain)
        currency, cur_detected = shopify_currency(domain)
        platform = "shopify"
    if not products:
        return {"ok": False, "domain": domain,
                "reason": "no public catalog found (not Shopify? connect WooCommerce keys or upload CSV)"}
    policies = shopify_policies(domain)
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
