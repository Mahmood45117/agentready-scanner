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

Adapters: Shopify (public /products.json), WooCommerce Store API (public, keyless — every Woo >= 3.6),
          WooCommerce REST (merchant keys, fallback for stores that disable the Store API).
Per-domain NON-SECRET enrichment (shipping string, returns, category, Q&A…) lives in feed_profiles.json.
CLI:  python3 feed_engine.py <domain>   -> writes feeds/<domain>.tsv (+ .tsv.gz, .csv.gz) + report JSON
"""
import csv, gzip, io, json, re, sys, os
import requests

UA = {"User-Agent": "Mozilla/5.0 (CanAIShopYou feed engine)"}
AVAIL = {True: "in_stock", False: "out_of_stock"}
HERE = os.path.dirname(os.path.abspath(__file__))

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


def _txt(html, cap=5000):
    t = re.sub(r"<[^>]+>", " ", html or "")
    t = re.sub(r"&nbsp;|&#160;", " ", t)
    t = re.sub(r"&amp;", "&", t); t = re.sub(r"&quot;", '"', t); t = re.sub(r"&#8217;|&rsquo;", "'", t)
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


def load_profile(domain):
    """Non-secret per-domain enrichment from feed_profiles.json (committed)."""
    try:
        return json.load(open(os.path.join(HERE, "feed_profiles.json"))).get(domain, {})
    except Exception:
        return {}


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
    "return_policy": ("/policies/refund-policy", "/returns-refunds/", "/returns/", "/refund-policy/", "/return-policy/"),
}
POLICY_SLUGS = {"seller_privacy_policy": ("privacy",), "seller_tos": ("terms", "conditions"), "return_policy": ("return", "refund")}


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
    """Discover privacy + terms (checkout gate) and the returns page (Shopify paths → WP paths → WP pages API)."""
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
    """Public catalog via /products.json (works on every standard Shopify store). Normalised in place."""
    products = []
    for page in range(1, max_pages + 1):
        data = _get(f"https://{domain}/products.json?limit=250&page={page}")
        if not data or not data.get("products"):
            break
        products += data["products"]
        if len(data["products"]) < 250:
            break
    for p in products:
        p["_brand"] = p.get("vendor") or ""
        p["_category"] = p.get("product_type") or ""
        p["_tags"] = p.get("tags") if isinstance(p.get("tags"), list) else [t.strip() for t in (p.get("tags") or "").split(",") if t.strip()]
        for v in p.get("variants", []):
            g = v.get("grams")
            if g:
                v["_weight"], v["_weight_unit"] = f"{int(g) / 453.592:.2f}", "lb"
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
    out = []
    for p in products:
        out.append({
            "id": p["id"], "title": p.get("name", ""), "handle": p.get("slug", ""),
            "body_html": p.get("description", ""), "vendor": "", "_url": p.get("permalink"),
            "images": [{"src": i.get("src")} for i in p.get("images", [])],
            "_category": " > ".join(c.get("name", "") for c in p.get("categories", [])[:1]),
            "options": [], "variants": [{
                "id": p["id"], "title": "Default Title", "price": p.get("price") or "0",
                "compare_at_price": p.get("regular_price") or None,
                "sku": p.get("sku", ""), "barcode": "", "available": p.get("stock_status") == "instock",
                "_weight": p.get("weight") or "", "_dims": p.get("dimensions") or {},
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
    Returns (products in Shopify-ish shape with _brand/_category/_attrs/_rating…, currency or None)."""
    products, currency = [], None
    for page in range(1, max_pages + 1):
        data = _get(f"https://{domain}/wp-json/wc/store/v1/products?per_page=100&page={page}")
        if not data or not isinstance(data, list):
            break
        products += data
        if len(data) < 100:
            break
    products = [p for p in products if p.get("is_purchasable", True) and not p.get("is_password_protected")]
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
        # non-variation attributes -> spec fields (material / color) + everything else kept for variant_dict context
        static_attrs = {a["name"]: ", ".join(t.get("name", "") for t in a.get("terms", []))
                        for a in p.get("attributes", []) if not a.get("has_variations") and a.get("name")}
        variants = []
        for v in p.get("variations") or []:
            detail = details.get(v["id"]) or {}
            dpr = detail.get("prices") or pr
            attrs = {a.get("name"): a.get("value") for a in v.get("attributes", []) if a.get("name")}
            var = {
                "id": v["id"], "title": ", ".join(attrs.get(n, "") for n in opt_names if attrs.get(n)) or "Default Title",
                "price": _minor(dpr.get("sale_price") or dpr.get("price"), unit),
                "compare_at_price": _minor(dpr.get("regular_price"), unit) if dpr.get("regular_price") else None,
                "sku": detail.get("sku") or "", "barcode": "",
                "available": detail.get("is_in_stock", p.get("is_in_stock", True)),
                "_weight": detail.get("weight") or "", "_dims": detail.get("dimensions") or {},
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
                "_weight": p.get("weight") or "", "_dims": p.get("dimensions") or {},
            }]
        cats = p.get("categories") or []
        out.append({
            "id": p["id"], "title": p.get("name", ""), "handle": p.get("slug", ""),
            "body_html": p.get("description") or p.get("short_description") or "",
            "vendor": "", "_url": p.get("permalink"),
            "images": [{"src": i.get("src")} for i in p.get("images", []) if i.get("src")],
            "options": [{"name": n} for n in opt_names], "variants": variants,
            "_brand": ", ".join(b.get("name", "") for b in (p.get("brands") or []))[:70],
            "_category": " > ".join(c.get("name", "") for c in cats[:1]),
            "_category_slug": (cats[0].get("slug") if cats else ""),
            "_tags": [t.get("name", "") for t in p.get("tags") or []],
            "_attrs": static_attrs,
            "_rating": p.get("average_rating"), "_reviews": p.get("review_count"),
            "_digital": not p.get("has_options", True) and False,   # Store API doesn't expose virtual; keep false
        })
    return out, currency


def _pick(d, words):
    for k, v in (d or {}).items():
        if any(w in k.lower() for w in words) and v:
            return v
    return ""


def build_rows(domain, products, brand_name, currency, policies, profile=None):
    prof = profile or {}
    rows, issues = [], {"no_identifier": 0, "no_image": 0, "no_description": 0, "truncated_title": 0, "short_description": 0}
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
        imgs = [i.get("src") for i in p.get("images", []) if i.get("src")]
        opt_names = [o.get("name") for o in p.get("options", []) if o.get("name")]
        many = len(p.get("variants", [])) > 1
        p_brand = (p.get("_brand") or prof.get("brand") or brand_name or "")[:70]
        cat = prof.get("product_category") or p.get("_category") or ""
        material = (_pick(p.get("_attrs"), MATERIAL_WORDS) or prof.get("material", ""))[:100]
        p_color = _pick(p.get("_attrs"), COLOR_WORDS)
        related = ""
        if rel_type:
            others = [i for i in by_cat.get(p.get("_category_slug") or p.get("_category") or "", []) if i != str(p["id"])]
            related = ",".join(others[:20])
        for v in p["variants"]:
            vt = v.get("title") or ""
            title = p["title"] if vt in ("", "Default Title") else f"{p['title']} - {vt}"
            if len(title) > 150:
                title = title[:150]; issues["truncated_title"] += 1
            desc = _txt(p.get("body_html"))
            if v.get("_desc"):
                desc = (v["_desc"] + " " + desc)[:5000]
            if not desc:
                desc = title; issues["no_description"] += 1
            elif len(desc) < 200:
                issues["short_description"] += 1
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
            v_size = _pick(vdict, SIZE_WORDS)
            # a real colour option on the variant wins; else the product's colour attribute; else a finish/shade option
            v_color = _pick(vdict, ("color", "colour")) or p_color or _pick(vdict, COLOR_WORDS)
            dims = v.get("_dims") or {}
            has_dims = all(dims.get(k) for k in ("length", "width", "height"))
            url = p.get("_url") or f"https://{domain}/products/{p['handle']}"
            v_img = v.get("_image") or (imgs[0] if imgs else "")
            extra = [i for i in imgs if i != v_img][:10]
            rows.append({
                "is_eligible_search": "true",
                "is_eligible_checkout": "true" if checkout_ok else "false",
                "is_ads_eligible": prof.get("is_ads_eligible", "false"),
                "item_id": str(v["id"]), "gtin": gtin, "mpn": mpn,
                "title": title, "description": desc,
                "url": url if not many else f"{url}?variant={v['id']}",
                "brand": p_brand, "condition": prof.get("condition", "new"),
                "product_category": cat, "material": material,
                "length": dims.get("length", "") if has_dims else "", "width": dims.get("width", "") if has_dims else "",
                "height": dims.get("height", "") if has_dims else "",
                "dimensions_unit": prof.get("dimensions_unit", "in") if has_dims else "",
                "weight": v.get("_weight", ""), "item_weight_unit": (v.get("_weight_unit") or prof.get("weight_unit", "lb")) if v.get("_weight") else "",
                "age_group": prof.get("age_group", ""),
                "image_url": v_img, "additional_image_urls": ",".join(extra), "video_url": prof.get("video_url", ""),
                "price": f"{cmp_at} {currency}" if on_sale else f"{price} {currency}",
                "sale_price": f"{price} {currency}" if on_sale else "",
                "availability": AVAIL.get(bool(v.get("available")), "unknown"), "availability_date": "",
                "group_id": str(p["id"]) if many else "",
                "listing_has_variations": "true" if many else "false",
                "variant_dict": json.dumps(vdict, ensure_ascii=False) if vdict else "",
                "item_group_title": p["title"][:150] if many else "",
                "color": (v_color or "")[:40], "size": (v_size or "")[:20],
                "shipping": ship, "is_digital": "true" if p.get("_digital") else "false",
                "seller_name": seller, "seller_url": prof.get("seller_url") or f"https://{domain}",
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
        if "<" in r["description"] and ">" in r["description"]: errors.append(f"row {i}: description contains HTML")
        if not re.match(r"^\d+(\.\d+)? [A-Z]{3}$", r["price"]): errors.append(f"row {i}: price '{r['price']}' must be 'amount CUR'")
        if r["sale_price"] and float(r["sale_price"].split()[0]) > float(r["price"].split()[0]):
            errors.append(f"row {i}: sale_price above price")
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


def run(domain, brand_name=None, woo_keys=None, outdir="feeds", check_urls=0):
    domain = re.sub(r"^https?://", "", domain.strip().lower()).split("/")[0]
    profile = load_profile(domain)
    brand_name = brand_name or profile.get("brand") or domain.split(".")[0].replace("-", " ").title()
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
    rows, issues, checkout_ok = build_rows(domain, products, brand_name, currency, policies, profile)
    tsv, gz = write_feed(domain, rows, outdir)
    gfeed = write_google_feed(domain, rows, outdir)
    v = validate(rows, check_urls=check_urls)
    report = {
        "ok": True, "domain": domain, "platform": platform,
        "products": len(products), "feed_rows": len(rows),
        "files": {"tsv": tsv, "tsv_gz": tsv[:-4] + ".tsv.gz", "csv_gz": gz, "google_tsv": gfeed},
        "currency": currency, "currency_detected": cur_detected,
        "search_eligible": True,
        "checkout_eligible": checkout_ok,
        "checkout_blockers": [] if checkout_ok else
            [k for k in ("seller_privacy_policy", "seller_tos") if not policies.get(k)],
        "data_quality": issues,
        "spec": v,
        "profile_applied": bool(profile),
    }
    with open(os.path.join(outdir, f"{domain}.report.json"), "w") as f:
        json.dump(report, f, indent=2)
    return report


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python3 feed_engine.py <domain> [--check-urls N]"); sys.exit(1)
    n = int(sys.argv[sys.argv.index("--check-urls") + 1]) if "--check-urls" in sys.argv else 0
    print(json.dumps(run(sys.argv[1], check_urls=n), indent=2))
