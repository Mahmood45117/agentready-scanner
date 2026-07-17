#!/usr/bin/env python3
"""Can AI Shop You? — agent-readiness scanner (v2, branded)
Free automated checks + Index + full-audit lead capture.
Usage:  .venv/bin/python app.py            -> web UI on http://localhost:8899
        .venv/bin/python app.py <domain>   -> CLI scan
"""
import json, re, sys
import requests
from flask import Flask, request, render_template_string

AUDIT_EMAIL = "mkb8630963@gmail.com"   # <- where "request full audit" clicks land
UA = "AgentReadyScanner/2.0 (+https://canaishopyou.com)"
TIMEOUT = 12

AI_BOTS = {
    "OAI-SearchBot":  ("ChatGPT search & shopping visibility", "FATAL — store is invisible in ChatGPT answers"),
    "ChatGPT-User":   ("live page fetches during ChatGPT sessions", "SEVERE — AI can't verify price/stock mid-conversation"),
    "PerplexityBot":  ("Perplexity search & shopping", "SEVERE — invisible on Perplexity"),
    "GPTBot":         ("OpenAI training crawler only", "OK to block — does NOT affect shopping visibility"),
    "Googlebot":      ("Google search + AI Mode grounding", "FATAL — invisible on Google surfaces"),
    "Bingbot":        ("Bing + Microsoft Copilot grounding", "SEVERE — weak/no Copilot presence"),
}

INDEX_ED1 = [
    ("hexclad.com", 95, "A", "Nearly perfect — positioned to own AI cookware recommendations"),
    ("allbirds.com", 95, "A", "Clean across all checks"),
    ("athleticbrewing.com", 95, "A", "Clean across all checks"),
    ("magicspoon.com", 95, "A", "Clean across all checks"),
    ("chubbiesshorts.com", 93, "A", "Healthiest of the original ten"),
    ("vuoriclothing.com", 92, "A", "Clean; catalog participation unclear"),
    ("bombas.com", 90, "A", "Healthy; catalog participation unclear"),
    ("huel.com", 77, "B", "Price data incomplete → hallucinated-price risk"),
    ("gymshark.com", 65, "C", "No product structured data · Cloudflare risk"),
    ("mudwtr.com", 65, "C", "No product structured data · Cloudflare risk"),
    ("brooklinen.com", 65, "C", "No product structured data · Cloudflare risk"),
    ("nutrafol.com", 60, "C", "No product structured data"),
    ("ridge.com", 57, "C", "No product structured data · Cloudflare risk"),
    ("graza.co", 57, "C", "No product structured data"),
    ("seed.com", 55, "C", "No product structured data"),
    ("ruggable.com", 55, "C", "No product structured data"),
    ("carawayhome.com", 40, "D", "Bot-wall starves AI of data · confirmed ChatGPT price hallucination"),
    ("drinkag1.com", 40, "D", "robots.txt unreachable · no product data"),
    ("rhone.com", 35, "D", "robots.txt unreachable · Cloudflare risk · no product data"),
    ("ourplace.com", 22, "F", "BLOCKS AI shopping bots in robots.txt — invisible in ChatGPT"),
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
*{box-sizing:border-box}body{font-family:-apple-system,'Segoe UI',sans-serif;max-width:860px;margin:0 auto;padding:0 20px 60px;background:#0a0d14;color:#e8eaf0}
a{color:#7c9aff}.nav{display:flex;justify-content:space-between;align-items:center;padding:22px 0;font-weight:700}
.nav .brand{font-size:1.15em;color:#fff;text-decoration:none}.nav .links a{margin-left:18px;font-weight:500;text-decoration:none;color:#8b93a7}
.hero{text-align:center;padding:40px 0 26px}.hero h1{font-size:2.6em;margin:0 0 10px;background:linear-gradient(90deg,#7c9aff,#4ade80);-webkit-background-clip:text;background-clip:text;color:transparent}
.hero p{color:#8b93a7;font-size:1.15em;margin:0 0 26px}
form.scan{display:flex;gap:10px;max-width:560px;margin:0 auto}
input[name=domain]{flex:1;padding:15px 18px;font-size:1.05em;border-radius:12px;border:1px solid #2a3040;background:#141926;color:#fff;outline:none}
button{padding:15px 30px;font-size:1.05em;font-weight:700;border-radius:12px;border:0;background:linear-gradient(90deg,#4f7cff,#6c5ce7);color:#fff;cursor:pointer}
.stat{color:#4ade80;font-weight:600}
.card{background:#141926;border:1px solid #232a3b;border-radius:14px;padding:20px;margin:14px 0}
.PASS{color:#4ade80}.FAIL{color:#ff5f56}.WARN{color:#ffbd2e}.INFO{color:#8b93a7}
.score{font-size:3.4em;font-weight:800}.grade{font-size:1.25em;color:#8b93a7}
.cta{background:linear-gradient(135deg,#1a2140,#141926);border:1px solid #34406b;text-align:center}
.cta h3{margin:4px 0 8px;font-size:1.35em}.cta p{color:#8b93a7;margin:0 0 16px}
.cta a.btn{display:inline-block;padding:14px 28px;border-radius:12px;background:linear-gradient(90deg,#4f7cff,#6c5ce7);color:#fff;font-weight:700;text-decoration:none}
table{width:100%;border-collapse:collapse}td,th{padding:10px 8px;text-align:left;border-bottom:1px solid #232a3b}th{color:#8b93a7;font-weight:600}
.gA{color:#4ade80;font-weight:800}.gB{color:#a3e635;font-weight:800}.gC{color:#ffbd2e;font-weight:800}.gD,.gF{color:#ff5f56;font-weight:800}
.foot{text-align:center;color:#525a6e;margin-top:44px;font-size:.9em}
"""

NAV = """<div class="nav"><a class="brand" href="/">🔍 Can AI Shop You?</a>
<span class="links"><a href="/">Scanner</a><a href="/index-report">The Index</a></span></div>"""

PAGE = """<!doctype html><html><head><title>Can AI Shop You? — AgentReady Scanner</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><style>""" + BASE_CSS + """</style></head><body>
""" + NAV + """
<div class="hero"><h1>AI assistants are already sending shoppers to stores. Is yours one of them?</h1>
<p>ChatGPT, Perplexity &amp; Copilot now recommend products to millions — traffic that's up <span class="stat">+1,324%</span> and converts <span class="stat">42% better</span>.<br>
We test how they see your store: what they say about you, whether they quote your prices right — <b>or make them up</b>.<br>
<b>8 of 10 leading DTC brands we scanned are broken for AI shopping.</b> Find out in 30 seconds:</p>
<form class="scan" method="post"><input name="domain" placeholder="yourstore.com" value="{{domain or ''}}" required>
<button>Scan free</button></form>
<p style="margin-top:14px;color:#525a6e;font-size:.9em">🔍 Scan the plumbing → 🤖 interrogate the AIs → 📈 get the fix roadmap, ranked by revenue impact<br>
<i>Coming soon: <b>CANI</b> — our AI shopper that walks your store like a customer and shows you where it gets stuck.</i></p></div>
{% if r %}
<div class="card"><span class="score">{{r.score}}/100</span> <span class="grade">grade {{r.grade}} — {{r.domain}}</span></div>
{% for name,status,pts,detail in r.checks %}<div class="card"><b class="{{status}}">{{status}}</b> &nbsp; <b>{{name}}</b> <span class="grade" style="font-size:.85em">({{pts}} pts)</span><br><span style="color:#8b93a7">{{detail}}</span></div>{% endfor %}
<div class="card cta"><h3>This was the free scan — the machine layer.</h3>
<p>The <b>full audit</b> tests your store live inside ChatGPT, Perplexity &amp; Copilot: what they actually say about you,<br>whether they quote your prices right (or hallucinate them), and your feed rails — with a prioritized fix roadmap.</p>
<a class="btn" href="mailto:""" + AUDIT_EMAIL + """?subject=Full%20audit%20request%20—%20{{r.domain}}%20({{r.score}}/100)&body=Scanned%20{{r.domain}}%20on%20canaishopyou.com%20—%20score%20{{r.score}}/100.%20I%20want%20the%20full%207-point%20audit.">Request the full audit →</a></div>
{% endif %}
<div class="foot">canaishopyou.com · independent agent-readiness audits &amp; the Agent-Ready Index</div>
</body></html>"""

INDEX_PAGE = """<!doctype html><html><head><title>The Agent-Ready Index — Edition #1</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><style>""" + BASE_CSS + """</style></head><body>
""" + NAV + """
<div class="hero"><h1>The Agent-Ready Index</h1>
<p><b>Edition #1 — July 2026 (updated).</b> Twenty-one leading DTC brands, scanned for AI-shopping readiness.<br>
Headline finding: <span class="stat">two-thirds score below A</span> — broken product data, blocked crawlers, invisible listings.</p></div>
<div class="card"><table><tr><th>#</th><th>Brand</th><th>Score</th><th>Grade</th><th>Key finding</th></tr>
{% for i,(d,s,g,note) in rows %}<tr><td>{{i}}</td><td><b>{{d}}</b></td><td>{{s}}/100</td><td class="g{{g}}">{{g}}</td><td style="color:#8b93a7">{{note}}</td></tr>{% endfor %}
</table></div>
<div class="card cta"><h3>Is your store on the wrong half of this table?</h3>
<p>Run the free scan — or request the full audit with live AI-surface testing.</p>
<a class="btn" href="/">Scan your store free →</a></div>
<div class="foot">Methodology: automated deterministic checks (robots/CDN/llms.txt/catalog rail/structured data). Edition #2: Q4 2026.</div>
</body></html>"""

app = Flask(__name__)

@app.route("/", methods=["GET", "POST"])
def index():
    r, domain = None, None
    if request.method == "POST":
        domain = request.form.get("domain", "")
        r = scan(domain)
        class O(dict): __getattr__ = dict.get
        r = O(r)
    return render_template_string(PAGE, r=r, domain=domain)

@app.route("/index-report")
def index_report():
    return render_template_string(INDEX_PAGE, rows=list(enumerate(INDEX_ED1, 1)))

if __name__ == "__main__":
    if len(sys.argv) > 1:
        res = scan(sys.argv[1])
        print(f"\n🔍 {res['domain']} — {res['score']}/100 (grade {res['grade']})")
        for name, status, pts, detail in res["checks"]:
            print(f"  [{status:4}] {name} ({pts}pts)\n         {detail}")
    else:
        app.run(port=8899)
