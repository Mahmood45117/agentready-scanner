#!/usr/bin/env python3
"""AgentReady Scanner v1 — can AI shopping assistants see this store?
Runs the deterministic agent-readiness checks on any store URL and scores them.
Usage:  .venv/bin/python app.py            -> web UI on http://localhost:8899
        .venv/bin/python app.py <domain>   -> CLI scan, prints report
"""
import json, re, sys, urllib.parse
import requests
from flask import Flask, request, render_template_string

UA = "AgentReadyScanner/1.0 (+agent-readiness audit; contact: owner)"
TIMEOUT = 12

AI_BOTS = {
    "OAI-SearchBot":  ("ChatGPT search & shopping visibility", "FATAL — store is invisible in ChatGPT answers"),
    "ChatGPT-User":   ("live page fetches during ChatGPT sessions", "SEVERE — AI can't verify price/stock mid-conversation"),
    "PerplexityBot":  ("Perplexity search & shopping", "SEVERE — invisible on Perplexity"),
    "GPTBot":         ("OpenAI training crawler only", "OK to block — does NOT affect shopping visibility"),
    "Googlebot":      ("Google search + AI Mode grounding", "FATAL — invisible on Google surfaces"),
    "Bingbot":        ("Bing + Microsoft Copilot grounding", "SEVERE — weak/no Copilot presence"),
}

def fetch(url):
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT, allow_redirects=True)
        return r
    except Exception:
        return None

def parse_robots(text):
    """Return {bot: 'allowed'|'blocked'|'unlisted'} based on robots.txt group rules."""
    groups, current_agents, rules = [], [], []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line: continue
        m = re.match(r"(?i)^(user-agent|disallow|allow)\s*:\s*(.*)$", line)
        if not m: continue
        key, val = m.group(1).lower(), m.group(2).strip()
        if key == "user-agent":
            if rules:
                groups.append((current_agents, rules)); current_agents, rules = [], []
            current_agents.append(val)
        else:
            rules.append((key, val))
    if current_agents or rules:
        groups.append((current_agents, rules))
    def status_for(bot):
        bot_l = bot.lower()
        best = None  # most specific match wins: exact name > *
        for agents, rls in groups:
            agents_l = [a.lower() for a in agents]
            if bot_l in agents_l: prio = 2
            elif "*" in agents_l: prio = 1
            else: continue
            if best is None or prio > best[0]:
                best = (prio, rls)
        if best is None: return "unlisted"
        blocked_root = any(k == "disallow" and v == "/" for k, v in best[1])
        return "blocked" if blocked_root else "allowed"
    return {bot: status_for(bot) for bot in AI_BOTS}

def scan(domain):
    domain = domain.strip().lower()
    domain = re.sub(r"^https?://", "", domain).split("/")[0]
    base = f"https://{domain}"
    checks, score = [], 0

    # CHECK 1 — robots.txt AI-bot access (40 pts)
    r = fetch(f"{base}/robots.txt")
    if r is None or r.status_code >= 400:
        checks.append(("Robots.txt AI-bot access", "WARN", 20,
                       f"robots.txt not reachable (HTTP {r.status_code if r else 'error'}) — bots default to allowed, but verify manually."))
        score += 20; bots = {}
    else:
        bots = parse_robots(r.text)
        fatal = [b for b in ("OAI-SearchBot", "PerplexityBot", "Googlebot", "Bingbot", "ChatGPT-User") if bots.get(b) == "blocked"]
        gpt_note = " (GPTBot blocked — training only, no sales impact)" if bots.get("GPTBot") == "blocked" else ""
        if fatal:
            detail = "; ".join(f"{b} BLOCKED → {AI_BOTS[b][1]}" for b in fatal)
            checks.append(("Robots.txt AI-bot access", "FAIL", 0, detail + gpt_note))
        else:
            checks.append(("Robots.txt AI-bot access", "PASS", 40,
                           "All shopping/search bots allowed" + gpt_note))
            score += 40

    # CHECK 2 — Cloudflare / CDN wall risk (10 pts)
    home = fetch(base)
    cf = False
    if home is not None:
        h = {k.lower(): v for k, v in home.headers.items()}
        cf = "cf-ray" in h or "cloudflare" in h.get("server", "").lower()
    if cf:
        checks.append(("CDN silent-block risk (Cloudflare)", "WARN", 5,
                       "Behind Cloudflare — AI crawlers are blocked BY DEFAULT for new zones since Jul 2025. Owner must verify AI-crawler settings in dashboard. High-value finding."))
        score += 5
    else:
        checks.append(("CDN silent-block risk", "PASS", 10, "No Cloudflare AI-blocking signature detected."))
        score += 10

    # CHECK 3 — llms.txt (5 pts, tiebreaker weight)
    l = fetch(f"{base}/llms.txt")
    if l is not None and l.status_code == 200 and len(l.text) > 20:
        checks.append(("llms.txt", "PASS", 5, "Present (low real-world impact today, signals AI-awareness)."))
        score += 5
    else:
        checks.append(("llms.txt", "INFO", 3, "Absent — like 97% of the web; cheap hedge to add, minimal impact."))
        score += 3

    # CHECK 4 — Shopify catalog rail (15 pts)
    is_shopify = home is not None and "cdn.shopify.com" in (home.text or "")
    pj = fetch(f"{base}/products.json?limit=5")
    pj_ok = False
    if pj is not None and pj.status_code == 200:
        try:
            pj_ok = len(pj.json().get("products", [])) > 0
        except Exception:
            pj_ok = False
    if is_shopify and pj_ok:
        checks.append(("Platform catalog rail (Shopify)", "PASS", 15,
                       "Shopify store with open product JSON — eligible for the Shopify Catalog rail into ChatGPT/Copilot/Google (on by default since Mar 2026). Verify merchant hasn't opted out."))
        score += 15
    elif is_shopify:
        checks.append(("Platform catalog rail (Shopify)", "WARN", 7,
                       "Shopify detected but products.json closed — catalog participation unclear; verify in admin."))
        score += 7
    else:
        checks.append(("Platform catalog rail", "INFO", 7,
                       "Not Shopify (or undetected) — no automatic catalog rail; feed rail (Google Merchant Center / OpenAI feed spec) matters more. Verify manually."))
        score += 7

    # CHECK 5 — structured data on a product page (30 pts)
    prod_url, jsonld_ok, offers_ok = None, False, False
    if pj_ok:
        try:
            handle = pj.json()["products"][0]["handle"]
            prod_url = f"{base}/products/{handle}"
        except Exception:
            prod_url = None
    if prod_url is None and home is not None:
        m = re.search(r'href="(/products/[^"?#]+)"', home.text or "")
        if m: prod_url = base + m.group(1)
    if prod_url:
        pr = fetch(prod_url)
        if pr is not None and pr.status_code == 200:
            for blob in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', pr.text, re.S | re.I):
                try:
                    data = json.loads(blob.strip())
                except Exception:
                    continue
                items = data if isinstance(data, list) else [data]
                for it in items:
                    graph = it.get("@graph", [it]) if isinstance(it, dict) else [it]
                    for node in graph:
                        if isinstance(node, dict) and "product" in str(node.get("@type", "")).lower():
                            jsonld_ok = True
                            if node.get("offers"): offers_ok = True
    if jsonld_ok and offers_ok:
        checks.append(("Product structured data (schema.org)", "PASS", 30,
                       f"Product JSON-LD with offers found on {prod_url} — AI can read name/price/availability reliably."))
        score += 30
    elif jsonld_ok:
        checks.append(("Product structured data", "WARN", 15,
                       f"Product JSON-LD found but no offers block on {prod_url} — price/stock may be misread → hallucinated-price risk."))
        score += 15
    else:
        checks.append(("Product structured data", "FAIL", 0,
                       f"No Product JSON-LD detected ({prod_url or 'no product page found'}) — AI must guess from raw HTML; wrong prices and missing listings likely."))

    grade = "A" if score >= 85 else "B" if score >= 70 else "C" if score >= 50 else "D" if score >= 35 else "F"
    return {"domain": domain, "score": score, "grade": grade, "checks": checks, "bots": bots,
            "note": "Automated deterministic checks only. Full audit adds live AI-surface testing (ChatGPT/Perplexity/Copilot answers, hallucinated-price hunt, feed rails) — the paid layer."}

PAGE = """<!doctype html><html><head><title>AgentReady Scanner</title><style>
body{font-family:-apple-system,sans-serif;max-width:760px;margin:40px auto;padding:0 16px;background:#0b0e14;color:#e6e6e6}
h1{font-size:1.6em}.sub{color:#8b93a7}input{width:70%;padding:12px;font-size:1em;border-radius:8px;border:1px solid #333;background:#151a23;color:#fff}
button{padding:12px 22px;font-size:1em;border-radius:8px;border:0;background:#4f7cff;color:#fff;cursor:pointer}
.card{background:#151a23;border:1px solid #262c3a;border-radius:12px;padding:18px;margin:14px 0}
.PASS{color:#3ddc84}.FAIL{color:#ff5f56}.WARN{color:#ffbd2e}.INFO{color:#8b93a7}
.score{font-size:3em;font-weight:800}.grade{font-size:1.2em;color:#8b93a7}
td{padding:4px 10px 4px 0;vertical-align:top}</style></head><body>
<h1>🔍 AgentReady Scanner</h1><p class="sub">Can AI shopping assistants see this store? Free automated checks — full audit goes deeper.</p>
<form method="post"><input name="domain" placeholder="store.com" value="{{domain or ''}}" required>
<button>Scan</button></form>
{% if r %}<div class="card"><span class="score">{{r.score}}/100</span> <span class="grade">grade {{r.grade}} — {{r.domain}}</span></div>
{% for name,status,pts,detail in r.checks %}<div class="card"><b class="{{status}}">{{status}}</b> &nbsp; <b>{{name}}</b> <span class="sub">({{pts}} pts)</span><br><span class="sub">{{detail}}</span></div>{% endfor %}
<div class="card sub">{{r.note}}</div>{% endif %}</body></html>"""

app = Flask(__name__)

@app.route("/", methods=["GET", "POST"])
def index():
    r, domain = None, None
    if request.method == "POST":
        domain = request.form.get("domain", "")
        r = scan(domain)
        r["checks"] = [tuple(c) for c in r["checks"]]
        class O(dict):
            __getattr__ = dict.get
        r = O(r)
    return render_template_string(PAGE, r=r, domain=domain)

if __name__ == "__main__":
    if len(sys.argv) > 1:
        res = scan(sys.argv[1])
        print(f"\n🔍 {res['domain']} — {res['score']}/100 (grade {res['grade']})")
        for name, status, pts, detail in res["checks"]:
            print(f"  [{status:4}] {name} ({pts}pts)\n         {detail}")
        print(f"\n  {res['note']}")
    else:
        app.run(port=8899, debug=False)
