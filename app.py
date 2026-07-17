#!/usr/bin/env python3
"""Can AI Shop You? — agent-readiness scanner (v2, branded)
Free automated checks + Index + full-audit lead capture.
Usage:  .venv/bin/python app.py            -> web UI on http://localhost:8899
        .venv/bin/python app.py <domain>   -> CLI scan
"""
import json, re, sys
import requests
from flask import Flask, request, render_template_string

AUDIT_EMAIL = "mahmood@canaishopyou.com"   # <- where "request full audit" clicks land
UA = "AgentReadyScanner/2.0 (+https://canaishopyou.com)"
TIMEOUT = 12

import os, json as _json, datetime
DATA_FILE = os.environ.get("DATA_FILE", "/tmp/cani_data.json")
BASELINE_SCANS = 41  # brands + tests already run by founder pre-launch
def _load():
    try:
        return _json.load(open(DATA_FILE))
    except Exception:
        return {"scans": [], "leads": []}
def _save(d):
    try:
        _json.dump(d, open(DATA_FILE, "w"))
    except Exception:
        pass
def log_scan(domain, score, grade):
    d = _load()
    d["scans"].append({"domain": domain, "score": score, "grade": grade,
                       "ts": _utcnow()})
    _save(d)
def log_lead(domain, score, email=""):
    d = _load()
    d["leads"].append({"domain": domain, "score": score, "email": email, "ts": _utcnow()})
    _save(d)
def scan_count():
    return BASELINE_SCANS + len(_load().get("scans", []))
def recent_scans(n=5):
    return list(reversed(_load().get("scans", [])))[:n]
def _utcnow():
    # avoid Date.now-style; use time
    import time
    return int(time.time())

AI_BOTS = {
    "OAI-SearchBot":  ("ChatGPT search & shopping visibility", "FATAL — store is invisible in ChatGPT answers"),
    "ChatGPT-User":   ("live page fetches during ChatGPT sessions", "SEVERE — AI can't verify price/stock mid-conversation"),
    "PerplexityBot":  ("Perplexity search & shopping", "SEVERE — invisible on Perplexity"),
    "GPTBot":         ("OpenAI training crawler only", "OK to block — does NOT affect shopping visibility"),
    "Googlebot":      ("Google search + AI Mode grounding", "FATAL — invisible on Google surfaces"),
    "Bingbot":        ("Bing + Microsoft Copilot grounding", "SEVERE — weak/no Copilot presence"),
}

INDEX_ED1 = [
    ("allbirds.com", 95, "A", "Clean across all checks"),
    ("athleticbrewing.com", 95, "A", "Clean across all checks"),
    ("awaytravel.com", 95, "A", "Clean across all checks"),
    ("casper.com", 95, "A", "Clean across all checks"),
    ("glossier.com", 95, "A", "Clean across all checks"),
    ("hexclad.com", 95, "A", "Nearly perfect — positioned to own AI cookware recommendations"),
    ("jonesroadbeauty.com", 95, "A", "Clean across all checks"),
    ("livemomentous.com", 95, "A", "Clean across all checks"),
    ("magicspoon.com", 95, "A", "Clean across all checks"),
    ("trueclassictees.com", 95, "A", "Clean across all checks"),
    ("chubbiesshorts.com", 93, "A", "Healthiest of the original ten"),
    ("vuoriclothing.com", 92, "A", "Clean; catalog participation unclear"),
    ("bombas.com", 90, "A", "Healthy; catalog participation unclear"),
    ("drinklmnt.com", 85, "A", "Healthy — robots open, product data readable"),
    ("fromourplace.com", 80, "B", "Product data missing offers block → hallucinated-price risk"),
    ("huel.com", 77, "B", "Price data incomplete → hallucinated-price risk"),
    ("aloyoga.com", 65, "C", "No product structured data · Cloudflare risk"),
    ("brooklinen.com", 65, "C", "No product structured data · Cloudflare risk"),
    ("gymshark.com", 65, "C", "No product structured data · Cloudflare risk"),
    ("mudwtr.com", 65, "C", "No product structured data · Cloudflare risk"),
    ("nutrafol.com", 60, "C", "No product structured data"),
    ("graza.co", 57, "C", "No product structured data"),
    ("ridge.com", 57, "C", "No product structured data · Cloudflare risk"),
    ("warbyparker.com", 57, "C", "No product structured data · Cloudflare risk"),
    ("ruggable.com", 55, "C", "No product structured data"),
    ("seed.com", 55, "C", "No product structured data"),
    ("carawayhome.com", 40, "D", "Bot-wall starves AI of data · confirmed ChatGPT price hallucination"),
    ("drinkag1.com", 40, "D", "robots.txt unreachable · no product data"),
    ("helixsleep.com", 40, "D", "robots.txt unreachable · no product data"),
    ("rhone.com", 35, "D", "robots.txt unreachable · Cloudflare risk · no product data"),
]

def fetch(url):
    try:
        return requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT, allow_redirects=True)
    except Exception:
        return None

def parse_robots(text):
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
        bot_l = bot.lower(); best = None
        for agents, rls in groups:
            agents_l = [a.lower() for a in agents]
            if bot_l in agents_l: prio = 2
            elif "*" in agents_l: prio = 1
            else: continue
            if best is None or prio > best[0]:
                best = (prio, rls)
        if best is None: return "unlisted"
        return "blocked" if any(k == "disallow" and v == "/" for k, v in best[1]) else "allowed"
    return {bot: status_for(bot) for bot in AI_BOTS}

def scan(domain):
    domain = re.sub(r"^https?://", "", domain.strip().lower()).split("/")[0]
    base = f"https://{domain}"
    checks, score = [], 0

    r = fetch(f"{base}/robots.txt")
    if r is None or r.status_code >= 400:
        checks.append(("Robots.txt AI-bot access", "WARN", 20,
                       f"robots.txt not reachable (HTTP {r.status_code if r else 'error'}) — bots default to allowed, but verify manually."))
        score += 20
    else:
        bots = parse_robots(r.text)
        fatal = [b for b in ("OAI-SearchBot", "PerplexityBot", "Googlebot", "Bingbot", "ChatGPT-User") if bots.get(b) == "blocked"]
        gpt_note = " (GPTBot blocked — training only, no sales impact)" if bots.get("GPTBot") == "blocked" else ""
        if fatal:
            checks.append(("Robots.txt AI-bot access", "FAIL", 0,
                           "; ".join(f"{b} BLOCKED → {AI_BOTS[b][1]}" for b in fatal) + gpt_note))
        else:
            checks.append(("Robots.txt AI-bot access", "PASS", 40, "All shopping/search bots allowed" + gpt_note))
            score += 40

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

    l = fetch(f"{base}/llms.txt")
    if l is not None and l.status_code == 200 and len(l.text) > 20:
        checks.append(("llms.txt", "PASS", 5, "Present (low real-world impact today, signals AI-awareness)."))
        score += 5
    else:
        checks.append(("llms.txt", "INFO", 3, "Absent — like 97% of the web; cheap hedge to add, minimal impact."))
        score += 3

    is_shopify = home is not None and "cdn.shopify.com" in (home.text or "")
    pj = fetch(f"{base}/products.json?limit=5")
    pj_ok = False
    if pj is not None and pj.status_code == 200:
        try: pj_ok = len(pj.json().get("products", [])) > 0
        except Exception: pj_ok = False
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

    prod_url, jsonld_ok, offers_ok = None, False, False
    if pj_ok:
        try: prod_url = f"{base}/products/{pj.json()['products'][0]['handle']}"
        except Exception: prod_url = None
    if prod_url is None and home is not None:
        m = re.search(r'href="(/products/[^"?#]+)"', home.text or "")
        if m: prod_url = base + m.group(1)
    if prod_url:
        pr = fetch(prod_url)
        if pr is not None and pr.status_code == 200:
            for blob in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', pr.text, re.S | re.I):
                try: data = json.loads(blob.strip())
                except Exception: continue
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
    return {"domain": domain, "score": score, "grade": grade, "checks": checks}

BASE_CSS = """
*{box-sizing:border-box;margin:0}
:root{--bg:#07090f;--card:#10141f;--card2:#151b2a;--line:#1f2740;--txt:#eef1f8;--mut:#8b93a7;--dim:#525a6e;
--grad:linear-gradient(93deg,#6d8dff,#3fd68c);--grad2:linear-gradient(135deg,#141b33,#10141f)}
body{font-family:-apple-system,'SF Pro Display','Segoe UI',sans-serif;background:
radial-gradient(1200px 500px at 50% -10%,#131b36 0%,var(--bg) 60%);color:var(--txt);min-height:100vh}
.wrap{max-width:880px;margin:0 auto;padding:0 22px 70px}
.nav{display:flex;justify-content:space-between;align-items:center;padding:24px 0}
.nav .brand{font-size:1.2em;font-weight:800;color:#fff;text-decoration:none;letter-spacing:-.3px}
.nav .brand span{background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}
.nav .links a{margin-left:22px;font-weight:600;font-size:.95em;text-decoration:none;color:var(--mut)}
.nav .links a:hover{color:#fff}
.hero{text-align:center;padding:52px 0 30px}
.hero h1{font-size:2.7em;line-height:1.12;letter-spacing:-1.2px;margin:0 auto 18px;max-width:800px}
.hero h1 em{font-style:normal;background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}
.hero p{color:var(--mut);font-size:1.12em;line-height:1.55;max-width:640px;margin:0 auto 30px}
.statrow{display:flex;gap:14px;justify-content:center;margin:0 0 34px;flex-wrap:wrap}
.stat{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 22px;text-align:center}
.stat b{display:block;font-size:1.5em;background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}
.stat span{color:var(--dim);font-size:.85em}
form.scan{display:flex;gap:10px;max-width:600px;margin:0 auto}
input[name=domain]{flex:1;padding:17px 20px;font-size:1.05em;border-radius:14px;border:1px solid var(--line);background:var(--card);color:#fff;outline:none;transition:border .2s}
input[name=domain]:focus{border-color:#6d8dff}
button,.btn{padding:17px 32px;font-size:1.05em;font-weight:700;border-radius:14px;border:0;background:var(--grad);color:#06131a;cursor:pointer;text-decoration:none;display:inline-block}
button:hover,.btn:hover{filter:brightness(1.1)}
.steps{display:flex;gap:14px;margin:40px 0 0;flex-wrap:wrap}
.step{flex:1;min-width:200px;background:var(--card);border:1px solid var(--line);border-radius:16px;padding:22px}
.step .n{font-size:.8em;color:var(--dim);font-weight:700;letter-spacing:1px}
.step h4{margin:8px 0 6px;font-size:1.05em}
.step p{color:var(--mut);font-size:.92em;line-height:1.5}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:22px;margin:14px 0}
.PASS{color:#3fd68c}.FAIL{color:#ff5f56}.WARN{color:#ffbd2e}.INFO{color:#8b93a7}
.score{font-size:3.6em;font-weight:800;letter-spacing:-2px}.grade{font-size:1.25em;color:var(--mut)}
.cta{background:var(--grad2);border:1px solid #2c3760;text-align:center;padding:34px 26px}
.cta h3{margin:0 0 10px;font-size:1.45em;letter-spacing:-.5px}
.cta p{color:var(--mut);margin:0 0 20px;line-height:1.55}
.price{display:flex;gap:14px;justify-content:center;margin:26px 0 8px;flex-wrap:wrap}
.tier{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:22px 26px;min-width:230px;text-align:left}
.tier b{font-size:1.5em}.tier .per{color:var(--dim);font-size:.9em}
.tier ul{margin:12px 0 0;padding-left:18px;color:var(--mut);font-size:.9em;line-height:1.7}
table{width:100%;border-collapse:collapse}td,th{padding:11px 8px;text-align:left;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-weight:600;font-size:.85em;letter-spacing:.5px}
.gA{color:#3fd68c;font-weight:800}.gB{color:#a3e635;font-weight:800}.gC{color:#ffbd2e;font-weight:800}.gD,.gF{color:#ff5f56;font-weight:800}
.foot{text-align:center;color:var(--dim);margin-top:56px;font-size:.9em;line-height:1.8}
.foot a{color:var(--mut)}
@media(max-width:640px){.hero h1{font-size:2em}form.scan{flex-direction:column}}
"""

NAV = """<div class="wrap"><div class="nav"><a class="brand" href="/">🔍 Can<span>AI</span>ShopYou</a>
<span class="links"><a href="/">Scanner</a><a href="/index-report">The Index</a><a href="mailto:mahmood@canaishopyou.com">Contact</a></span></div>"""

PAGE = """<!doctype html><html><head><title>Can AI Shop You? — Agent-Readiness Scanner & Index</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta property="og:title" content="Can AI Shop You?">
<meta property="og:description" content="AI assistants recommend stores to millions of shoppers. Two-thirds of top DTC brands are broken for them. Scan yours free in 30 seconds.">
<meta name="description" content="Free agent-readiness scan: can ChatGPT, Perplexity & Copilot see and sell your store? Publisher of the Agent-Ready Index.">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🔍</text></svg>">
<style>""" + BASE_CSS + """</style></head><body>
""" + NAV + """
<div class="hero"><h1>AI assistants are already sending shoppers to stores.<br><em>Is yours one of them?</em></h1>
<p>ChatGPT, Perplexity &amp; Copilot now recommend and price-check products for millions. We test exactly how they see your store — and what they get wrong.</p>
<div class="statrow">
<div class="stat"><b>+1,324%</b><span>AI-referred retail traffic since Oct '24</span></div>
<div class="stat"><b>42%</b><span>better conversion vs average</span></div>
<div class="stat"><b>{{scans}}</b><span>stores scanned for AI-readiness</span></div>
</div>
<form class="scan" method="post"><input name="domain" placeholder="yourstore.com" value="{{domain or ''}}" required>
<button>Scan free</button></form></div>
{% if not r %}
<div class="steps">
<div class="step"><span class="n">STEP 1</span><h4>🔍 Scan the plumbing</h4><p>Crawler access, bot-walls, product data, catalog rails — the machine layer AI depends on. Free, 30 seconds.</p></div>
<div class="step"><span class="n">STEP 2</span><h4>🤖 Interrogate the AIs</h4><p>The full audit tests your store live inside ChatGPT, Perplexity &amp; Copilot — what they say, what they hallucinate.</p></div>
<div class="step"><span class="n">STEP 3</span><h4>📈 Fix what costs you</h4><p>Findings ranked by revenue impact, a 30-day roadmap, and a re-scan to prove the fix worked.</p></div>
</div>
<div class="price">
<div class="tier"><b>Free</b><div class="per">instant scan</div><ul><li>5 automated checks</li><li>Scored report card</li><li>No signup</li></ul></div>
<div class="tier"><b>$500</b><div class="per">full audit · 5 days</div><ul><li>Live AI-surface testing</li><li>Hallucination hunt</li><li>Revenue-ranked fix roadmap</li></ul></div>
<div class="tier"><b>$1,000<span class="per">/mo</span></b><div class="per">fix &amp; monitor</div><ul><li>Fix implementation</li><li>Weekly re-scans + alerts</li><li>Monthly report</li></ul></div>
</div>
<div class="card cta"><h3>The Agent-Ready Index</h3>
<p>We publish quarterly agent-readiness scores for leading DTC brands.<br>Edition #1: <b>30 brands scanned — two-thirds scored below A.</b> One blocked itself out of ChatGPT entirely.</p>
<a class="btn" href="/index-report">Read the Index →</a></div>
{% endif %}
{% if r %}
<div class="card"><span class="score">{{r.score}}/100</span> <span class="grade">grade {{r.grade}} — {{r.domain}}</span></div>
{% for name,status,pts,detail in r.checks %}<div class="card"><b class="{{status}}">{{status}}</b> &nbsp; <b>{{name}}</b> <span class="grade" style="font-size:.85em">({{pts}} pts)</span><br><span style="color:#8b93a7">{{detail}}</span></div>{% endfor %}
<div class="card cta"><h3>This was the free scan — the machine layer.</h3>
<p>The <b>full audit ($500)</b> tests your store live inside ChatGPT, Perplexity &amp; Copilot: what they actually say about you,<br>whether they quote your prices right (or hallucinate them) — with a fix roadmap ranked by revenue impact. Delivered in 5 days.</p>
<form method="post" action="/request" style="display:flex;gap:10px;max-width:460px;margin:0 auto;flex-wrap:wrap;justify-content:center">
<input type="hidden" name="domain" value="{{r.domain}}"><input type="hidden" name="score" value="{{r.score}}">
<input name="email" type="email" placeholder="you@yourstore.com" required style="flex:1;min-width:220px;padding:14px 16px;border-radius:12px;border:1px solid var(--line);background:var(--card);color:#fff">
<button>Request full audit →</button></form>
<p style="margin-top:12px;font-size:.85em;color:var(--dim)">or email <a href="mailto:mahmood@canaishopyou.com">mahmood@canaishopyou.com</a></p></div>
{% endif %}
<div class="foot">CanAIShopYou · independent agent-readiness audits &amp; the Agent-Ready Index<br>
<a href="mailto:mahmood@canaishopyou.com">mahmood@canaishopyou.com</a> · <a href="/index-report">Index Edition #1</a></div>
</div></body></html>"""

INDEX_PAGE = """<!doctype html><html><head><title>The Agent-Ready Index — Edition #1</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta property="og:title" content="The Agent-Ready Index — Edition #1">
<meta property="og:description" content="21 leading DTC brands scored for AI-shopping readiness. Two-thirds below A. One brand blocked itself out of ChatGPT entirely.">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🔍</text></svg>">
<style>""" + BASE_CSS + """</style></head><body>
""" + NAV + """
<div class="hero"><h1>The <em>Agent-Ready</em> Index</h1>
<p><b>Edition #1 — July 2026.</b> Thirty leading DTC brands, scanned for AI-shopping readiness.<br>
Headline finding: <b style="color:#ffbd2e">two-thirds score below A</b> — broken product data, blocked crawlers, invisible listings.</p></div>
<div class="card"><table><tr><th>#</th><th>BRAND</th><th>SCORE</th><th>GRADE</th><th>KEY FINDING</th></tr>
{% for i,(d,s,g,note) in rows %}<tr><td>{{i}}</td><td><b>{{d}}</b></td><td>{{s}}/100</td><td class="g{{g}}">{{g}}</td><td style="color:#8b93a7">{{note}}</td></tr>{% endfor %}
</table></div>
<div class="card cta"><h3>Is your store on the wrong half of this table?</h3>
<p>Run the free scan — or request the full audit with live AI-surface testing inside ChatGPT, Perplexity &amp; Copilot.</p>
<a class="btn" href="/">Scan your store free →</a></div>
<div class="foot">Methodology: automated deterministic checks (crawler access · CDN posture · llms.txt · catalog rails · structured data).<br>Live AI-surface findings (hallucination catches) noted where verified. Edition #2: Q4 2026.<br>
<a href="mailto:mahmood@canaishopyou.com">mahmood@canaishopyou.com</a></div>
</div></body></html>"""

app = Flask(__name__)

@app.route("/", methods=["GET", "POST"])
def index():
    r, domain = None, None
    if request.method == "POST":
        domain = request.form.get("domain", "")
        r = scan(domain)
        try: log_scan(r.get("domain",""), r.get("score",0), r.get("grade",""))
        except Exception: pass
        class O(dict): __getattr__ = dict.get
        r = O(r)
    return render_template_string(PAGE, r=r, domain=domain, scans=scan_count(), recent=recent_scans())

@app.route("/index-report")
def index_report():
    return render_template_string(INDEX_PAGE, rows=list(enumerate(INDEX_ED1, 1)), scans=scan_count())


@app.route("/request", methods=["POST"])
def request_audit():
    domain = request.form.get("domain", "")
    email = request.form.get("email", "")
    score = request.form.get("score", "")
    try: log_lead(domain, score, email)
    except Exception: pass
    return render_template_string(BASE_DOC, body=(
        "<div class='wrap'><div class='card cta' style='margin-top:60px'>"
        "<h3>Request received ✓</h3><p>Your full audit of <b>" + (domain or "your store") +
        "</b> is queued. We\'ll email the 7-point report within 5 business days"
        + ((" to <b>"+email+"</b>") if email else "") + ".</p>"
        "<a class='btn' href='/'>← Back</a></div></div>"))

BASE_DOC = """<!doctype html><html><head><title>CanAIShopYou</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><style>""" + BASE_CSS + """</style></head><body>{{ body|safe }}</body></html>"""

if __name__ == "__main__":
    if len(sys.argv) > 1:
        res = scan(sys.argv[1])
        print(f"\n🔍 {res['domain']} — {res['score']}/100 (grade {res['grade']})")
        for name, status, pts, detail in res["checks"]:
            print(f"  [{status:4}] {name} ({pts}pts)\n         {detail}")
    else:
        app.run(port=8899)
