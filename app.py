#!/usr/bin/env python3
"""Can AI Shop You? — agent-readiness scanner (v2, branded)
Free automated checks + Index + full-audit lead capture.
Usage:  .venv/bin/python app.py            -> web UI on http://localhost:8899
        .venv/bin/python app.py <domain>   -> CLI scan
"""
import json, re, sys
import requests
from flask import Flask, request, render_template_string, redirect, Response

AUDIT_EMAIL = "mahmood@canaishopyou.com"   # <- where "request full audit" clicks land
UA = "AgentReadyScanner/2.0 (+https://canaishopyou.com)"
TIMEOUT = 12

import os, json as _json, datetime
try:
    import sys as _sys; _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from ai_findings import FINDINGS   # the bundled corpus — 56 real validated findings
except Exception:
    FINDINGS = {}
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
def log_lead(domain, score, email="", extra="", kind="scan"):
    d = _load()
    d["leads"].append({"domain": domain, "score": score, "email": email, "extra": extra, "kind": kind, "ts": _utcnow()})
    _save(d)
def scan_count():
    return BASELINE_SCANS + len(_load().get("scans", []))
def recent_scans(n=5):
    return list(reversed(_load().get("scans", [])))[:n]
def _utcnow():
    # avoid Date.now-style; use time
    import time
    return int(time.time())

# ─────────── LIVE AI-VISIBILITY TEST — gated: email + per-IP + daily cap ───────────
import html as _html
AI_SYS   = ("You are a shopping assistant answering a real shopper. Recommend real, specific brands/products "
            "and state prices only if you actually know them. Answer as you genuinely would.")
AI_MODEL = os.environ.get("AI_MODEL", "claude-sonnet-5")
AI_REPS  = int(os.environ.get("AI_REPS", "1"))          # 3 formulations x REPS calls/test — public teaser stays fast & under any worker timeout; deep reps happen in the paid engagement
AI_DAILY_CAP = int(os.environ.get("AI_DAILY_CAP", "150"))  # worst-case ~$10/day, then falls back to lead capture
AI_IP_HOURLY = int(os.environ.get("AI_IP_HOURLY", "2"))

def _ai_key():
    if os.environ.get("ANTHROPIC_API_KEY"): return os.environ["ANTHROPIC_API_KEY"]
    here = os.path.dirname(os.path.abspath(__file__))
    for p in ("../cani/.anthropic_key", "cani/.anthropic_key"):
        try:
            k = open(os.path.join(here, p)).read().strip()
            if k.startswith("sk-ant"): return k
        except Exception: pass
    return None

def _ai_extract(text):
    for s in [m.start() for m in re.finditer(r"\{", text)]:
        try: obj, _ = _json.JSONDecoder().raw_decode(text[s:])
        except Exception: continue
        if isinstance(obj, dict) and any(k in obj for k in ("mentions_brand", "winner", "brand_price")): return obj
    return {}

def _ai_ask(client, qtext, brand):
    # Force a structured tool call instead of parsing JSON out of prose — 100% reliable
    # extraction (no truncation), and the tiny output keeps each call fast (~2-3s).
    tool = {"name": "record_pick",
            "description": "Record the brand you would genuinely recommend to this shopper.",
            "input_schema": {"type": "object", "properties": {
                "mentions_brand": {"type": "boolean", "description": f"Would you recommend {brand}?"},
                "winner": {"type": "string", "description": "The single #1 brand you would steer this shopper to"},
                "competitors": {"type": "array", "items": {"type": "string"}, "description": "Other brands you would name"}},
                "required": ["mentions_brand", "winner"]}}
    m = client.messages.create(model=AI_MODEL, max_tokens=400, system=AI_SYS, tools=[tool],
                               tool_choice={"type": "tool", "name": "record_pick"},
                               messages=[{"role": "user", "content": qtext + " Give your genuine pick."}])
    d = {}
    for b in m.content:
        if getattr(b, "type", "") == "tool_use" and isinstance(getattr(b, "input", None), dict):
            d = dict(b.input); break
    d.setdefault("_answer", ""); return d

def _ai_forms(brand, niche, rivals):
    cmp = " vs ".join([brand] + (rivals or [])[:2]) if rivals else brand
    return [("broad-purchase", f"Best {niche} brands right now — list the specific brands you'd recommend, with rough prices if you know them."),
            ("use-case",       f"I'm someone shopping for {niche}. What should I buy? Name the specific brands you'd point me to."),
            ("direct-compare", f"{cmp} — for someone buying {niche}, which should I buy and why?")]

def _selfm(a, b):
    a, b = (a or "").lower(), (b or "").lower(); return bool(a) and (a == b or a in b or b in a)

# ── Multi-engine: query the AI assistants shoppers actually use (Claude / ChatGPT / Perplexity) ──
AI_OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
AI_PPLX_MODEL   = os.environ.get("PERPLEXITY_MODEL", "sonar")

def _file_key(*files):
    here = os.path.dirname(os.path.abspath(__file__))
    for p in files:
        try:
            k = open(os.path.join(here, p)).read().strip()
            if k: return k
        except Exception: pass
    return None
def _openai_key(): return os.environ.get("OPENAI_API_KEY") or _file_key("../cani/.openai_key", "cani/.openai_key")
def _pplx_key():   return os.environ.get("PERPLEXITY_API_KEY") or _file_key("../cani/.perplexity_key", "cani/.perplexity_key")

def _oa_pick(qtext, brand):
    # ChatGPT via OpenAI — forced function call returns the structured pick in one shot.
    key = _openai_key()
    if not key: return None
    tools = [{"type": "function", "function": {"name": "record_pick",
              "description": f"Record the brand you'd genuinely recommend. mentions_brand = would you recommend {brand}?",
              "parameters": {"type": "object", "properties": {
                  "mentions_brand": {"type": "boolean"},
                  "winner": {"type": "string", "description": "the single #1 brand you steer this shopper to; empty if none"}},
                  "required": ["mentions_brand", "winner"]}}}]
    r = requests.post("https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": AI_OPENAI_MODEL, "max_tokens": 300,
              "messages": [{"role": "system", "content": AI_SYS}, {"role": "user", "content": qtext + " Give your genuine pick."}],
              "tools": tools, "tool_choice": {"type": "function", "function": {"name": "record_pick"}}},
        timeout=15)
    r.raise_for_status()
    return _json.loads(r.json()["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])

def _pplx_answer(qtext):
    # Perplexity is web-grounded — closest to a real "AI shopper." Prose answer; Claude extracts the pick.
    key = _pplx_key()
    if not key: return None
    r = requests.post("https://api.perplexity.ai/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": AI_PPLX_MODEL, "max_tokens": 500,
              "messages": [{"role": "system", "content": AI_SYS}, {"role": "user", "content": qtext}]},
        timeout=15)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

def _extract_pick(client, answer, brand):
    # Claude forced-tool extraction of the pick from any engine's prose answer.
    if not answer: return {}
    tool = {"name": "record_pick", "description": "Extract the shopping recommendation from an answer.",
            "input_schema": {"type": "object", "properties": {
                "mentions_brand": {"type": "boolean", "description": f"Does the answer recommend {brand}?"},
                "winner": {"type": "string", "description": "the single #1 brand the answer steers the shopper to; empty if none"}},
                "required": ["mentions_brand", "winner"]}}
    m = client.messages.create(model=AI_MODEL, max_tokens=150,
        system="You extract structured facts from a shopping answer. Be literal; add no opinions.",
        tools=[tool], tool_choice={"type": "tool", "name": "record_pick"},
        messages=[{"role": "user", "content": f'A shopper asked an AI to recommend brands in {brand}\'s category. It answered:\n\n"""{answer[:1500]}"""\n\nRecord the single brand it recommends #1, and whether it recommends {brand}.'}])
    for b in m.content:
        if getattr(b, "type", "") == "tool_use" and isinstance(getattr(b, "input", None), dict): return dict(b.input)
    return {}

def run_ai_test(domain, niche, rivals=None):
    if not _ai_key(): return {"ok": False, "reason": "engine offline"}
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=_ai_key(), timeout=20.0, max_retries=1)
    except Exception as e:
        return {"ok": False, "reason": f"engine error: {type(e).__name__}"}
    brand = domain.split(".")[0].replace("-", " ").title()
    all_forms = _ai_forms(brand, niche, rivals)
    # Engines: Claude always; ChatGPT / Perplexity join only if their key is set (so default == Claude-only).
    engine_defs = [("Claude", lambda q: _ai_ask(client, q, brand))]
    if _openai_key(): engine_defs.append(("ChatGPT", lambda q: _oa_pick(q, brand)))
    if _pplx_key():   engine_defs.append(("Perplexity", lambda q: _extract_pick(client, _pplx_answer(q), brand)))
    multi = len(engine_defs) > 1
    forms = all_forms[:1] if multi else all_forms   # 1 question x N engines when multi (bounds latency); 3 ways when Claude-only

    def _run_engine(name, picker):
        rows = []
        for fk, prompt in forms:
            try: r = picker(prompt) or {}
            except Exception as ex:
                import sys as _s; print(f"[engine {name}] {type(ex).__name__}: {str(ex)[:120]}", file=_s.stderr); r = {"_err": type(ex).__name__}
            w = (r.get("winner") or "").strip()
            lost = bool(w and not _selfm(w, brand))
            rows.append({"f": fk, "winner": w if lost else "", "prompt": prompt,
                         "omit": r.get("mentions_brand") is False, "err": r.get("_err")})
        wins = [x["winner"] for x in rows if x["winner"]]
        top = max(set(wins), key=wins.count) if wins else ""
        return {"engine": name, "rows": rows, "lost_ct": len(wins), "total": len(forms),
                "competitor": top, "ok": sum(1 for x in rows if x["err"]) < len(forms)}

    engines = []
    try:
        from concurrent.futures import ThreadPoolExecutor, wait
        ex = ThreadPoolExecutor(max_workers=len(engine_defs))
        futs = [ex.submit(_run_engine, n, p) for n, p in engine_defs]
        done, _ = wait(futs, timeout=26)                 # hard latency bound; a stalled engine is dropped
        for f in done:
            try:
                e = f.result()
                if e and e.get("ok"): engines.append(e)
            except Exception: pass
        ex.shutdown(wait=False)
    except Exception:
        for n, p in engine_defs:
            try:
                e = _run_engine(n, p)
                if e and e.get("ok"): engines.append(e)
            except Exception: pass
    if not engines: return {"ok": False, "reason": "engines unavailable"}
    order = {"Claude": 0, "ChatGPT": 1, "Perplexity": 2}
    engines.sort(key=lambda e: order.get(e["engine"], 9))

    def _eng_lost(e): return e["lost_ct"] > e["total"] / 2
    n = len(engines); nlost = sum(1 for e in engines if _eng_lost(e))
    verdict = "SURVIVES" if nlost == n else ("PARTIAL" if nlost >= 1 else "COLLAPSES")
    comps = [e["competitor"] for e in engines if e["competitor"]]
    top = max(set(comps), key=comps.count) if comps else ""
    return {"ok": True, "brand": brand, "niche": niche, "verdict": verdict, "competitor": top,
            "engines": engines, "n_engines": n, "engines_lost": nlost,
            "runs_lost": sum(e["lost_ct"] for e in engines), "runs_total": sum(e["total"] for e in engines),
            "date": datetime.date.today().isoformat()}

def _ai_allowed(ip):
    d = _load(); now = _utcnow(); runs = d.get("ai_runs", [])
    if len([x for x in runs if now - x["ts"] < 86400]) >= AI_DAILY_CAP: return False, "daily"
    if len([x for x in runs if x.get("ip") == ip and now - x["ts"] < 3600]) >= AI_IP_HOURLY: return False, "ip"
    return True, ""

def _ai_log_run(ip):
    d = _load(); d.setdefault("ai_runs", []).append({"ip": ip, "ts": _utcnow()})
    d["ai_runs"] = d["ai_runs"][-3000:]; _save(d)

def _ai_fix_rows(niche, competitor):
    c = _html.escape(competitor or "the incumbent"); n = _html.escape(niche)
    steps = [("Fix technical readability", "robots access, server-rendered content, product schema — so a crawler can read you at all", "hours"),
             (f"Build answer-content for '{n}'", "rewrite the pages AI reads (Princeton GEO formula: stats, cited sources, comparisons) targeting the exact questions you lose", "days"),
             ("Get into the AI shopping feeds", "OpenAI product feed, Perplexity / Bing Merchant, Shopify Catalog", "setup"),
             (f"Get into the sources AI cites for '{n}'", f"the 'best {n}' listicles, Reddit and reviews that make it reach for {c}", "weeks, ongoing"),
             ("Re-run this exact test in 30 days", "measured before/after — you pay the back half only if the number moves", "—")]
    return "".join(f"<tr><td style='padding:7px;border-bottom:1px solid #eee'><b>{_html.escape(t)}</b></td>"
                   f"<td style='padding:7px;border-bottom:1px solid #eee'>{d}</td>"
                   f"<td style='padding:7px;border-bottom:1px solid #eee;white-space:nowrap;color:#666'>{e}</td></tr>" for t, d, e in steps)

def render_ai_result(res, domain, email):
    dom = _html.escape(domain); comp = _html.escape(res.get("competitor") or "a competitor"); niche = _html.escape(res.get("niche") or "")
    engines = res.get("engines", []); n = res.get("n_engines", len(engines))
    elist = ", ".join(e["engine"] for e in engines) or "AI"
    def _eng_lost(e): return e["lost_ct"] > e["total"] / 2
    nlost = res.get("engines_lost", sum(1 for e in engines if _eng_lost(e))); multi = n > 1
    if res["verdict"] == "SURVIVES":
        head = ((f"Every AI engine we tested — <b>{_html.escape(elist)}</b> — sent shoppers looking for the best "
                 f"<b>{niche}</b> to <b style='color:#b3261e'>{comp}</b>, not you.") if multi else
                (f"Ask <b>{_html.escape(elist)}</b> for the best <b>{niche}</b> and it recommended "
                 f"<b style='color:#b3261e'>{comp}</b> over you — reproduced across every way we asked."))
        bg = "#fff6f6"
    elif res["verdict"] == "PARTIAL":
        head = (f"<b>{comp}</b> is beating you on some AI engines but not all — <b>{nlost} of {n}</b> "
                f"engines steered <b>{niche}</b> shoppers to a competitor."); bg = "#fffaf0"
    else:
        head = (f"You held up — none of the engines we tested (<b>{_html.escape(elist)}</b>) consistently "
                f"preferred a competitor for <b>{niche}</b>. This one's on your side."); bg = "#f2fbf5"
    erows = ""
    for e in engines:
        ways = f" <span style='color:#888'>({e['lost_ct']} of {e['total']} ways)</span>" if e["total"] > 1 else ""
        if e["competitor"] and _eng_lost(e):
            cell = f"<span style='color:#b3261e'>→ recommended {_html.escape(e['competitor'])}</span>{ways}"
        elif e["competitor"]:
            cell = f"<span style='color:#8a6d00'>mixed — {_html.escape(e['competitor'])} in {e['lost_ct']} of {e['total']}</span>"
        else:
            cell = "<span style='color:#0a7d3c'>held up — recommended you / no clear competitor</span>"
        erows += (f"<tr><td style='padding:9px;border-bottom:1px solid #eee;font-weight:700;white-space:nowrap'>{_html.escape(e['engine'])}</td>"
                  f"<td style='padding:9px;border-bottom:1px solid #eee'>{cell}</td></tr>")
    qs = engines[0]["rows"] if engines else []
    ql = "".join(f"<li style='margin:4px 0'><code style='font-size:.82em'>{_html.escape(x['prompt'])}</code></li>" for x in qs)
    fix = ("" if res["verdict"] == "COLLAPSES" else
           f"<div class='card'><h3>How we fix it</h3><table style='width:100%;border-collapse:collapse;font-size:.9em'>{_ai_fix_rows(res.get('niche',''), res.get('competitor',''))}</table>"
           f"<p style='margin-top:10px'>Half up front — the other half only if the 30-day retest shows the number moved. No agency offers that.</p>"
           f"<a class='btn' href='mailto:mahmood@canaishopyou.com?subject=Fix%20plan%20for%20{dom}'>Get my fix plan →</a></div>")
    api_eng = [e["engine"] for e in engines if e["engine"] in ("Claude", "ChatGPT")]
    notes = []
    if api_eng: notes.append(f"{' &amp; '.join(api_eng)} via API (no live browsing)")
    if any(e["engine"] == "Perplexity" for e in engines): notes.append("Perplexity is web-grounded")
    label = (f"Controlled test across <b>{_html.escape(elist)}</b>" + (" · one question per engine" if multi else " · asked 3 ways")
             + ". " + ("; ".join(notes) + ". " if notes else "") + "Paste any question below into the real thing to check.")
    body = (f"<div class='wrap'><div class='card' style='background:{bg};border:1px solid #eadada;margin-top:40px'>"
            f"<div style='color:#0a7d3c;font-weight:700;font-size:12px'>LIVE AI-VISIBILITY TEST · {res['date']}</div>"
            f"<h2 style='margin:.2em 0'>{dom}</h2><p style='font-size:1.05em'>{head}</p>"
            f"<table style='width:100%;border-collapse:collapse;font-size:.95em;margin-top:8px'>"
            f"<tr><th style='text-align:left;padding:9px'>AI engine</th><th style='text-align:left;padding:9px'>What it told shoppers</th></tr>{erows}</table>"
            f"<details style='margin-top:10px'><summary style='cursor:pointer;font-size:.85em;color:#555'>The exact question(s) we asked — verify it yourself</summary>"
            f"<ul style='margin:8px 0 0'>{ql}</ul></details>"
            f"<p style='font-size:.8em;color:#666;margin-top:8px'>{label}</p></div>"
            f"{fix}<p style='text-align:center'><a href='/'>← test another</a></p></div>")
    return body

AI_BOTS = {
    "OAI-SearchBot":  ("ChatGPT search & shopping visibility", "FATAL — store is invisible in ChatGPT answers"),
    "ChatGPT-User":   ("live page fetches during ChatGPT sessions", "SEVERE — AI can't verify price/stock mid-conversation"),
    "PerplexityBot":  ("Perplexity search & shopping", "SEVERE — invisible on Perplexity"),
    "GPTBot":         ("OpenAI training crawler only", "OK to block — does NOT affect shopping visibility"),
    "Googlebot":      ("Google search + AI Mode grounding", "FATAL — invisible on Google surfaces"),
    "Bingbot":        ("Bing + Microsoft Copilot grounding", "SEVERE — weak/no Copilot presence"),
}

# NOTE: this Index is generated by scale_prospects.py (Check-0 + deterministic scan over
# prospects.csv) and copied from scanner/index_generated.py. To grow it: add brands to
# prospects.csv, run `python scale_prospects.py`, and paste the new blocks here.
INDEX_ED1 = [
    ("hexclad.com", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("materialkitchen.com", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("flybyjing.com", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("brightland.co", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("graza.co", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("misen.com", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("casper.com", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("bearaby.com", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("avocadogreenmattress.com", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("greatjonesgoods.com", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("floydhome.com", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("coyuchi.com", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("buffy.co", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("marinelayer.com", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("allbirds.com", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("trueclassictees.com", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("koio.co", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("ritual.com", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("nisolo.com", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("livemomentous.com", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("magicspoon.com", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("thursdayboots.com", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("liquiddeath.com", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("jenis.com", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("athleticbrewing.com", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("davidprotein.com", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("jonesroadbeauty.com", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("iliabeauty.com", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("glossier.com", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("tower28beauty.com", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("meritbeauty.com", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("versedskin.com", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("rarebeauty.com", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("cuyana.com", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("monos.com", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("paireyewear.com", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("awaytravel.com", 95, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("bollandbranch.com", 93, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("chubbiesshorts.com", 93, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("cutsclothing.com", 93, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("harrys.com", 93, "A", "Healthy — verify Cloudflare isn't blocking AI crawlers"),
    ("vuoriclothing.com", 92, "A", "Healthy — Platform catalog rail (Shopify)"),
    ("buckmason.com", 90, "A", "Healthy — Platform catalog rail (Shopify)"),
    ("nomadgoods.com", 87, "A", "verify Cloudflare isn't blocking AI crawlers"),
    ("atoms.com", 85, "A", "verify Cloudflare isn't blocking AI crawlers"),
    ("drinklmnt.com", 85, "A", "verify Cloudflare isn't blocking AI crawlers"),
    ("fromourplace.com", 80, "B", "verify Cloudflare isn't blocking AI crawlers"),
    ("tuftandneedle.com", 80, "B", "verify Cloudflare isn't blocking AI crawlers"),
    ("parachutehome.com", 80, "B", "verify Cloudflare isn't blocking AI crawlers"),
    ("tenthousand.cc", 80, "B", "verify Cloudflare isn't blocking AI crawlers"),
    ("fahertybrand.com", 80, "B", "verify Cloudflare isn't blocking AI crawlers"),
    ("outdoorvoices.com", 80, "B", "verify Cloudflare isn't blocking AI crawlers"),
    ("rothys.com", 80, "B", "verify Cloudflare isn't blocking AI crawlers"),
    ("drinkolipop.com", 80, "B", "verify Cloudflare isn't blocking AI crawlers"),
    ("drinkpoppi.com", 80, "B", "verify Cloudflare isn't blocking AI crawlers"),
    ("transparentlabs.com", 80, "B", "verify Cloudflare isn't blocking AI crawlers"),
    ("kosas.com", 80, "B", "verify Cloudflare isn't blocking AI crawlers"),
    ("madeincookware.com", 77, "B", "Platform catalog rail (Shopify)"),
    ("huel.com", 77, "B", "Platform catalog rail (Shopify)"),
    ("bombas.com", 75, "B", "Platform catalog rail (Shopify)"),
    ("peakdesign.com", 73, "B", "robots.txt blocks AI bots"),
    ("burrow.com", 72, "B", "verify Cloudflare isn't blocking AI crawlers"),
    ("brooklinen.com", 65, "C", "no product structured data → hallucinated-price risk"),
    ("gymshark.com", 65, "C", "no product structured data → hallucinated-price risk"),
    ("aloyoga.com", 65, "C", "no product structured data → hallucinated-price risk"),
    ("dagnedover.com", 65, "C", "no product structured data → hallucinated-price risk"),
    ("nectarsleep.com", 62, "C", "no product structured data → hallucinated-price risk"),
    ("spotandtango.com", 62, "C", "no product structured data → hallucinated-price risk"),
    ("article.com", 60, "C", "no product structured data → hallucinated-price risk"),
    ("nutrafol.com", 60, "C", "no product structured data → hallucinated-price risk"),
    ("bellroy.com", 60, "C", "no product structured data → hallucinated-price risk"),
    ("purple.com", 57, "C", "no product structured data → hallucinated-price risk"),
    ("ridge.com", 57, "C", "Bot-wall (HTTP 403) — AI shoppers can't read the store"),
    ("warbyparker.com", 57, "C", "Bot-wall (HTTP 403) — AI shoppers can't read the store"),
    ("ruggable.com", 55, "C", "no product structured data → hallucinated-price risk"),
    ("westernrise.com", 55, "C", "no product structured data → hallucinated-price risk"),
    ("seed.com", 55, "C", "Bot-wall (HTTP 403) — AI shoppers can't read the store"),
    ("youthtothepeople.com", 55, "C", "Bot-wall (HTTP 403) — AI shoppers can't read the store"),
    ("drunkelephant.com", 55, "C", "no product structured data → hallucinated-price risk"),
    ("beis.com", 55, "C", "no product structured data → hallucinated-price risk"),
    ("carawayhome.com", 40, "D", "Bot-wall (HTTP 403) — AI shoppers can't read the store"),
    ("saatva.com", 40, "D", "Bot-wall (HTTP 403) — AI shoppers can't read the store"),
    ("helixsleep.com", 40, "D", "Bot-wall (HTTP 403) — AI shoppers can't read the store"),
    ("bonobos.com", 40, "D", "Bot-wall (HTTP 420) — AI shoppers can't read the store"),
    ("mizzenandmain.com", 40, "D", "Bot-wall (HTTP 429) — AI shoppers can't read the store"),
    ("drinkag1.com", 40, "D", "Bot-wall (HTTP 429) — AI shoppers can't read the store"),
    ("rhone.com", 35, "D", "Bot-wall (HTTP 403) — AI shoppers can't read the store"),
    ("legionathletics.com", 35, "D", "Bot-wall (HTTP 403) — AI shoppers can't read the store"),
    ("omsom.com", 35, "D", "no product structured data → hallucinated-price risk"),
    ("thefarmersdog.com", 35, "D", "Bot-wall (HTTP 403) — AI shoppers can't read the store"),
]

# category -> [domains] (for the competitive comparison table on report pages)
CATEGORIES = {
    "Cookware": ["hexclad.com", "materialkitchen.com", "brightland.co", "graza.co", "misen.com", "greatjonesgoods.com", "fromourplace.com", "madeincookware.com", "carawayhome.com"],
    "Food & beverage": ["flybyjing.com", "magicspoon.com", "liquiddeath.com", "jenis.com", "athleticbrewing.com", "davidprotein.com", "drinkolipop.com", "drinkpoppi.com", "omsom.com"],
    "Mattresses": ["casper.com", "bearaby.com", "avocadogreenmattress.com", "tuftandneedle.com", "nectarsleep.com", "purple.com", "saatva.com", "helixsleep.com"],
    "Home & bedding": ["floydhome.com", "coyuchi.com", "buffy.co", "bollandbranch.com", "parachutehome.com", "burrow.com", "brooklinen.com", "article.com", "ruggable.com"],
    "Apparel & activewear": ["marinelayer.com", "trueclassictees.com", "chubbiesshorts.com", "cutsclothing.com", "vuoriclothing.com", "buckmason.com", "tenthousand.cc", "fahertybrand.com", "outdoorvoices.com", "bombas.com", "gymshark.com", "aloyoga.com", "westernrise.com", "bonobos.com", "mizzenandmain.com", "rhone.com"],
    "Footwear": ["allbirds.com", "koio.co", "nisolo.com", "thursdayboots.com", "atoms.com", "rothys.com"],
    "Supplements & wellness": ["ritual.com", "livemomentous.com", "drinklmnt.com", "transparentlabs.com", "huel.com", "nutrafol.com", "seed.com", "drinkag1.com", "legionathletics.com"],
    "Beauty & skincare": ["jonesroadbeauty.com", "iliabeauty.com", "glossier.com", "tower28beauty.com", "meritbeauty.com", "versedskin.com", "rarebeauty.com", "kosas.com", "youthtothepeople.com", "drunkelephant.com"],
    "Bags & accessories": ["cuyana.com", "monos.com", "awaytravel.com", "dagnedover.com", "bellroy.com", "ridge.com", "beis.com"],
    "Eyewear": ["paireyewear.com", "warbyparker.com"],
    "Grooming": ["harrys.com"],
    "Electronics & gear": ["nomadgoods.com", "peakdesign.com"],
    "Pet": ["spotandtango.com", "thefarmersdog.com"],
}
# Prefer the generated Index (scale_prospects.py → index_generated.py) when present;
# the inline list above is the fallback. This makes growing the Index a one-file upload.
try:
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    from index_generated import INDEX_ED1, CATEGORIES  # noqa: F811
except Exception:
    pass

_INDEX = {d: (s, g, note) for d, s, g, note in INDEX_ED1}
_RANK = {d: i for i, (d, *_) in enumerate(INDEX_ED1, 1)}

def category_of(domain):
    for cat, doms in CATEGORIES.items():
        if domain in doms:
            return cat, doms
    return None, []

def brand_report(domain):
    """Public teaser data for an Index brand. Whitelisted to INDEX only (no arbitrary scans)."""
    if domain not in _INDEX:
        return None
    score, grade, note = _INDEX[domain]
    cat, doms = category_of(domain)
    peers = sorted([(d, _INDEX[d][0], _INDEX[d][1]) for d in doms if d in _INDEX],
                   key=lambda x: -x[1]) if cat else []
    return {"domain": domain, "score": score, "grade": grade, "note": note,
            "rank": _RANK.get(domain), "total": len(INDEX_ED1),
            "category": cat, "peers": peers, "tested_date": "July 2026"}

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
:root{--bg:#f5f7fc;--card:#ffffff;--ink:#0c1526;--mut:#57617a;--dim:#98a1b5;--hair:#edf0f7;--line:#e2e7f1;
--accent:#4f56ff;--accent2:#7b5cff;--grad:linear-gradient(135deg,#4f6bff 0%,#7b5cff 100%);
--gAb:#e7f7ef;--gAf:#07926a;--gAl:#c6ecda;--gBb:#eef7dc;--gBf:#5a8a10;--gBl:#dcecba;
--gCb:#fdf3e1;--gCf:#b26a08;--gCl:#f6ddb8;--gDb:#fdecea;--gDf:#d43a2c;--gDl:#f6cfca;
--sh:0 1px 2px rgba(15,23,42,.04),0 5px 16px -4px rgba(15,23,42,.08);--shlg:0 2px 8px rgba(15,23,42,.05),0 24px 50px -18px rgba(15,23,42,.16)}
html{scroll-behavior:smooth}
body{font-family:-apple-system,'SF Pro Display','Segoe UI',Inter,Helvetica,Arial,sans-serif;color:var(--ink);min-height:100vh;-webkit-font-smoothing:antialiased;letter-spacing:-.1px;
background:radial-gradient(1200px 560px at 50% -14%,#ffffff 0%,var(--bg) 60%) fixed}
body::before{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;opacity:.6;background-image:radial-gradient(circle at 18% 8%,rgba(79,107,255,.08),transparent 42%),radial-gradient(circle at 88% 4%,rgba(123,92,255,.07),transparent 40%)}
button,input,select,textarea{font-family:inherit}
.wrap{max-width:1080px;margin:0 auto;padding:0 24px 110px}
.nav{display:flex;justify-content:space-between;align-items:center;padding:22px 0}
.nav .brand{font-size:1.24em;font-weight:800;color:var(--ink);text-decoration:none;letter-spacing:-.5px}
.nav .brand span{background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}
.nav .links a{margin-left:26px;font-weight:550;font-size:.95em;text-decoration:none;color:var(--mut);transition:color .15s}
.nav .links a:hover{color:var(--ink)}
.hero{text-align:center;padding:64px 0 30px;animation:fadeUp .6s cubic-bezier(.2,.7,.2,1) both}
.hero h1{font-size:clamp(2.35em,5.3vw,3.95em);line-height:1.04;letter-spacing:-1.9px;font-weight:800;margin:0 auto 22px;max-width:920px}
.hero h1 em{font-style:normal;background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}
.hero p{color:var(--mut);font-size:1.16em;line-height:1.62;max-width:640px;margin:0 auto 30px;font-weight:400}
.statrow{display:flex;gap:14px;justify-content:center;margin:0 0 30px;flex-wrap:wrap}
.stat{background:var(--card);border:1px solid var(--hair);border-radius:15px;padding:16px 26px;text-align:center;box-shadow:var(--sh)}
.stat b{display:block;font-size:1.72em;font-weight:800;letter-spacing:-.6px;background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}
.stat span{color:var(--mut);font-size:.82em;font-weight:450}
form.scan{display:flex;gap:10px;max-width:560px;margin:0 auto}
input[name=domain],input[type=email]{flex:1;padding:15px 18px;font-size:1.05em;font-weight:500;border-radius:12px;border:1px solid var(--line);background:#fff;color:var(--ink);outline:none;box-shadow:0 1px 2px rgba(15,23,42,.04);transition:border .15s,box-shadow .15s}
input::placeholder{color:var(--dim)}
input[name=domain]:focus,input[type=email]:focus{border-color:var(--accent);box-shadow:0 0 0 4px rgba(79,86,255,.13)}
button,.btn{padding:15px 26px;font-size:1em;font-weight:650;border-radius:12px;border:0;background:var(--grad);color:#fff;cursor:pointer;text-decoration:none;display:inline-block;box-shadow:0 7px 18px -5px rgba(79,86,255,.5);transition:transform .14s,box-shadow .14s,filter .14s}
button:hover,.btn:hover{transform:translateY(-1px);box-shadow:0 12px 28px -6px rgba(123,92,255,.6);filter:brightness(1.05)}
.engines{display:flex;gap:9px 24px;justify-content:center;align-items:center;flex-wrap:wrap;margin:34px auto 0;max-width:640px;color:var(--dim);font-size:.8em;font-weight:600;letter-spacing:.2px}
.engines .lbl{width:100%;text-transform:uppercase;font-size:.86em;letter-spacing:1px;margin-bottom:2px;color:var(--dim)}
.engines b{color:var(--mut);font-weight:750}
.steps{display:flex;gap:18px;margin:56px 0 0;flex-wrap:wrap}
.step{flex:1;min-width:220px;background:var(--card);border:1px solid var(--hair);border-radius:18px;padding:28px;box-shadow:var(--sh);transition:transform .2s,box-shadow .2s}
.step:hover{transform:translateY(-3px);box-shadow:var(--shlg)}
.step .n{font-size:.72em;font-weight:700;letter-spacing:.8px;text-transform:uppercase;background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}
.step h4{margin:12px 0 8px;font-size:1.12em;font-weight:700}
.step p{color:var(--mut);font-size:.93em;line-height:1.62;font-weight:400}
.card{background:var(--card);border:1px solid var(--hair);border-radius:18px;padding:26px;margin:16px 0;box-shadow:var(--sh)}
.pill{display:inline-block;padding:4px 12px;border-radius:8px;font-weight:700;font-size:.8em;border:1px solid transparent;vertical-align:middle;letter-spacing:.2px}
.pA{background:var(--gAb);color:var(--gAf);border-color:var(--gAl)}.pB{background:var(--gBb);color:var(--gBf);border-color:var(--gBl)}
.pC{background:var(--gCb);color:var(--gCf);border-color:var(--gCl)}.pD,.pF{background:var(--gDb);color:var(--gDf);border-color:var(--gDl)}
.PASS,.FAIL,.WARN,.INFO{display:inline-block;padding:3px 10px;border-radius:7px;font-size:.74em;font-weight:700;letter-spacing:.3px;border:1px solid transparent}
.PASS{background:var(--gAb);color:var(--gAf);border-color:var(--gAl)}.FAIL{background:var(--gDb);color:var(--gDf);border-color:var(--gDl)}
.WARN{background:var(--gCb);color:var(--gCf);border-color:var(--gCl)}.INFO{background:#f1f3f8;color:var(--mut);border-color:var(--hair)}
.score{font-size:3.7em;font-weight:800;letter-spacing:-2px;line-height:1}
.cA{color:var(--gAf)}.cB{color:var(--gBf)}.cC{color:var(--gCf)}.cD,.cF{color:var(--gDf)}
.gA{color:var(--gAf);font-weight:700}.gB{color:var(--gBf);font-weight:700}.gC{color:var(--gCf);font-weight:700}.gD,.gF{color:var(--gDf);font-weight:700}
.grade{font-size:1.05em;color:var(--mut);font-weight:600}
.dom{font-size:1.1em;font-weight:700}.outof{font-size:.42em;font-weight:700;color:var(--dim);letter-spacing:0}
.cta{background:linear-gradient(135deg,#4f6bff 0%,#7b5cff 100%);color:#fff;border:0;border-radius:22px;text-align:center;padding:46px 32px;box-shadow:0 24px 50px -18px rgba(79,86,255,.55)}
.cta h3{margin:0 0 12px;font-size:1.6em;letter-spacing:-.6px;font-weight:800;color:#fff}
.cta p{color:rgba(255,255,255,.86);margin:0 0 24px;line-height:1.62;font-weight:400}
.cta a.btn,.cta button{background:#fff;color:var(--accent);box-shadow:0 8px 22px -6px rgba(0,0,0,.25)}
.cta a.btn:hover,.cta button:hover{background:#fff;filter:brightness(1);transform:translateY(-1px);box-shadow:0 12px 28px -6px rgba(0,0,0,.3)}
.cta input[type=email]{background:rgba(255,255,255,.16);border:1px solid rgba(255,255,255,.3);color:#fff}
.cta input::placeholder{color:rgba(255,255,255,.7)}
.price{display:flex;gap:18px;justify-content:center;margin:32px 0 8px;flex-wrap:wrap}
.tier{background:var(--card);border:1px solid var(--hair);border-radius:18px;padding:26px;min-width:236px;text-align:left;box-shadow:var(--sh)}
.tier:nth-child(2){border:1.5px solid var(--accent);box-shadow:0 18px 42px -16px rgba(79,86,255,.34)}
.tier b{font-size:1.7em;font-weight:800;letter-spacing:-.6px}.tier .per{color:var(--dim);font-size:.82em;font-weight:450}
.tier ul{margin:14px 0 0;padding-left:18px;color:var(--mut);font-size:.9em;line-height:1.85;font-weight:400}
table{width:100%;border-collapse:collapse}
td,th{padding:14px 10px;text-align:left}
th{color:var(--mut);font-weight:600;font-size:.76em;letter-spacing:.5px;text-transform:uppercase;border-bottom:1px solid var(--line)}
td{border-bottom:1px solid var(--hair)}
td a{color:var(--ink);text-decoration:none;font-weight:600;border-bottom:1px solid transparent;transition:color .12s}
td a:hover{color:var(--accent);border-bottom-color:var(--accent)}
.repcard{background:var(--card);border:1px solid var(--hair);border-radius:24px;padding:48px 34px;text-align:center;margin:24px 0;box-shadow:var(--shlg)}
.repcard .score{font-size:clamp(3.4em,9vw,5em)}
.rank{color:var(--mut);font-size:1.05em;margin-top:14px;font-weight:450}
.yourrow{background:#eef1ff!important}
.yourrow td{color:var(--ink)!important;font-weight:700}
.foot{text-align:center;color:var(--dim);margin-top:66px;font-size:.86em;line-height:1.9;font-weight:400}
.foot a{color:var(--mut);font-weight:550}
@keyframes fadeUp{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
@media(max-width:640px){.hero h1{letter-spacing:-1px}form.scan{flex-direction:column}}
"""

NAV = """<div class="wrap"><div class="nav"><a class="brand" href="/">🔍 Can<span>AI</span>ShopYou</a>
<span class="links"><a href="/">Connect</a><a href="/how-it-works">How it works</a><a href="/about">About</a><a href="mailto:mahmood@canaishopyou.com">Contact</a></span></div>"""

PAGE = """<!doctype html><html><head><title>CanAIShopYou — Get your store into AI shopping</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta property="og:title" content="Get your store into AI shopping — feed, application, agent checkout">
<meta property="og:description" content="Shopify stores got into ChatGPT Shopping automatically. We get everyone else in: spec-compliant product feeds, merchant application, and agent-ready checkout.">
<meta name="description" content="People now buy inside ChatGPT. CanAIShopYou plugs stores into AI shopping: we build and host your OpenAI product feed, handle your merchant application, and run agent checkout.">
<meta name="google-site-verification" content="W_rODKils0f-T6_EvgJ2IX1lK8MGErJIpzL1aIey4L4" />
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Organization","name":"CanAIShopYou",
"url":"https://canaishopyou.com","email":"mahmood@canaishopyou.com",
"description":"The on-ramp to AI shopping: product feeds, merchant onboarding and agent checkout that connect online stores to ChatGPT Shopping and AI agents.",
"founder":{"@type":"Person","name":"Mahmood"},
"address":{"@type":"PostalAddress","addressLocality":"Multan","addressCountry":"PK"},
"knowsAbout":["AI search visibility","generative engine optimization","AI commerce testing"],
"contactPoint":{"@type":"ContactPoint","email":"mahmood@canaishopyou.com","contactType":"customer support"}}
</script>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🔍</text></svg>">
<style>""" + BASE_CSS + """</style></head><body>
""" + NAV + """
<div class="hero"><h1>People are buying inside ChatGPT.<br><em>We plug your store into that channel.</em></h1>
<p>AI shopping runs on two things your store probably doesn't have: a spec-compliant product feed and an agent-ready checkout. Shopify and Etsy stores were wired in automatically &mdash; everyone else was left out. We build, host and run that layer for you, so AI assistants can find your products <b>and complete the purchase</b>.</p>
<div class="statrow">
<div class="stat"><b>Live now</b><span>shoppers check out in-chat on ChatGPT</span></div>
<div class="stat"><b>Feed + checkout</b><span>the two things the channel requires</span></div>
<div class="stat"><b>~60 seconds</b><span>to see your store's real eligibility</span></div>
</div>
<form class="scan" method="post" action="/connect" style="max-width:640px;flex-wrap:wrap">
<input name="domain" placeholder="yourstore.com" value="{{domain or ''}}" required>
<input name="email" type="email" placeholder="you@yourstore.com" required>
<button>Connect my store &rarr;</button></form>
<p style="margin-top:10px;font-size:.82em;color:var(--dim)">Free eligibility check &mdash; we generate your actual feed on the spot and show exactly what blocks you.</p>
<div class="engines"><span class="lbl">The surfaces we plug you into</span><b>ChatGPT Shopping</b><b>Instant Checkout</b><b>Perplexity Merchants</b><b>MCP agents</b></div></div>
{% if not r %}
<div class="card" style="border-left:3px solid var(--accent)">
<div style="font-size:.72em;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--accent)">Why now</div>
<p style="margin:10px 0 6px;font-size:1.08em;line-height:1.55;color:var(--ink)">OpenAI opened ChatGPT Shopping and in-chat checkout to merchants. <b>Shopify and Etsy sellers were integrated automatically.</b> Everyone else needs a spec-compliant product feed, crawler access, an approved merchant application &mdash; and, for in-chat purchases, the Agentic Commerce Protocol with delegated payments.</p>
<p style="margin:0;color:var(--mut);font-size:.95em">There's no self-serve portal yet. That gap &mdash; between stores that got the channel for free and stores locked out of it &mdash; is exactly the work we do.</p>
</div>
<div class="steps">
<div class="step"><span class="n">STEP 1</span><h4>🔌 Connect your store</h4><p>Shopify connects instantly from your domain. WooCommerce, BigCommerce and custom stacks connect with read-only API keys. No code on your side.</p></div>
<div class="step"><span class="n">STEP 2</span><h4>📦 We build &amp; host your feed</h4><p>Your catalog, transformed to OpenAI's exact product-feed spec &mdash; validated, hosted on our infrastructure, auto-refreshed so price and stock stay true.</p></div>
<div class="step"><span class="n">STEP 3</span><h4>📨 We handle the application</h4><p>Crawler access, policy URLs, checkout-eligibility fields, and your ChatGPT merchant application &mdash; prepared and submitted for you.</p></div>
<div class="step"><span class="n">STEP 4</span><h4>🛒 Agent checkout</h4><p>Our hosted checkout endpoint speaks the Agentic Commerce Protocol to ChatGPT, charges your Stripe, and drops the order into your store. Rolling out to connected merchants.</p></div>
</div>
<div class="price">
<div class="tier"><b>Free</b><div class="per">eligibility check</div><ul><li>Your real feed generated on the spot</li><li>Exactly what blocks search &amp; checkout</li><li>No signup, no code</li></ul></div>
<div class="tier"><b>Launch</b><div class="per">into the pipeline</div><ul><li>Feed built, validated &amp; hosted</li><li>Auto-refresh + crawler access</li><li>Merchant application handled</li></ul></div>
<div class="tier"><b>Infrastructure</b><div class="per">monthly + % of agent sales</div><ul><li>Always-on feed hosting &amp; monitoring</li><li>Agent checkout endpoint (ACP + Stripe)</li><li>You earn from the channel &mdash; so do we</li></ul></div>
</div>
<div class="card cta"><h3>Your Shopify competitors are already in.</h3>
<p>The AI shopping channel is open now and the tooling gap won't last. Connect your store, see your eligibility in a minute, and get in while it's early.</p>
<a class="btn" href="mailto:mahmood@canaishopyou.com?subject=Connect%20my%20store">Talk to us &rarr;</a></div>
{% endif %}
{% if r %}
<div class="card" style="display:flex;align-items:center;gap:16px;flex-wrap:wrap"><span class="score c{{r.grade}}">{{r.score}}<span class="outof">/100</span></span> <span class="pill p{{r.grade}}" style="font-size:1em;padding:6px 16px">GRADE {{r.grade}}</span> <span class="dom">{{r.domain}}</span></div>
{% for name,status,pts,detail in r.checks %}<div class="card"><span class="{{status}}">{{status}}</span> &nbsp; <b>{{name}}</b> <span class="grade" style="font-size:.85em">({{pts}} pts)</span><br><span style="color:var(--mut)">{{detail}}</span></div>{% endfor %}
{% if ai %}
<div class="card" style="border:1px solid #f0cccc;background:#fff6f6">
<h3 style="margin-top:0">What AI actually tells shoppers about {{r.domain}}</h3>
{% if ai.verdict == 'SURVIVES' %}
<p style="font-size:1.06em">Ask AI for the best <b>{{ai.niche}}</b> and it recommended <b style="color:#b3261e">{{ai.competitor}}</b> over you in <b>{{ai.runs_lost}} of {{ai.runs_total}} runs</b> — reproduced across every way we asked.</p>
{% elif ai.verdict == 'PARTIAL' %}
<p>Ask AI for the best <b>{{ai.niche}}</b> and the answer was <b>mixed</b> — a competitor was preferred in some phrasings, not all.</p>
{% else %}
<p style="color:#0a7d3c">Ask AI for the best <b>{{ai.niche}}</b> and you actually held up — no consistent competitor preference. (We test both ways; this one's on your side.)</p>
{% endif %}
<table style="width:100%;border-collapse:collapse;font-size:.9em;margin-top:6px">
{% for t in ai.tests %}<tr><td style="padding:7px;border-bottom:1px solid #eee;white-space:nowrap"><b>{{t.f}}</b></td><td style="padding:7px;border-bottom:1px solid #eee">{% if t.competitor %}<span style="color:#b3261e">chose {{t.competitor}}</span>{% else %}held up{% endif %}</td><td style="padding:7px;border-bottom:1px solid #eee"><code style="font-size:.82em">{{t.prompt}}</code></td></tr>{% endfor %}
</table>
<p style="font-size:.83em;color:var(--mut);margin-top:8px">Controlled test · {{ai.date}} · reproduced. Paste any prompt into ChatGPT or Claude and check it yourself.</p></div>
{% endif %}
<div class="card cta">
{% if ai and ai.verdict == 'SURVIVES' %}
<h3>AI is sending your buyers to {{ai.competitor}}. Here's how we fix it.</h3>
<p>We rebuild the content AI reads for '{{ai.niche}}', get you into the product feeds and sources it cites, then <b>re-run this exact test in 30 days</b> to prove it moved. Half up front — the rest only if the number changes.</p>
{% else %}
<h3>See what AI says about your brand — free.</h3>
<p>We run the real questions your customers ask an AI, reproduced with the exact prompts, and show you where a competitor is ranked above you. Then we fix it and re-measure. Nothing published about your brand — ever.</p>
{% endif %}
{% if ai and ai.verdict == 'SURVIVES' %}
<form method="post" action="/request" style="display:flex;gap:10px;max-width:460px;margin:0 auto;flex-wrap:wrap;justify-content:center">
<input type="hidden" name="domain" value="{{r.domain}}"><input type="hidden" name="score" value="{{r.score}}">
<input name="email" type="email" placeholder="you@yourstore.com" required style="flex:1;min-width:220px">
<button>Get my fix plan →</button></form>
{% else %}
<form method="post" action="/ai-test" style="display:flex;gap:10px;max-width:460px;margin:0 auto;flex-wrap:wrap;justify-content:center">
<input type="hidden" name="domain" value="{{r.domain}}">
<input name="niche" placeholder="what you sell — e.g. fresh cat food" required style="flex:1 1 100%;min-width:220px">
<input name="email" type="email" placeholder="you@yourstore.com" required style="flex:1;min-width:220px">
<button>Run my live AI test →</button></form>
<p style="margin-top:8px;font-size:.8em;color:var(--dim)">Live · reproduced across 3 phrasings · ~15s · you can verify every prompt</p>
{% endif %}
<p style="margin-top:12px;font-size:.85em;color:var(--dim)">or email <a href="mailto:mahmood@canaishopyou.com">mahmood@canaishopyou.com</a></p></div>
{% endif %}
<div class="foot">CanAIShopYou · the on-ramp to AI shopping — feeds, merchant onboarding &amp; agent checkout · Multan, Pakistan<br>
<a href="/about">About</a> · <a href="/privacy">Privacy</a> · <a href="/terms">Terms</a> · <a href="/how-it-works">How it works</a> · <a href="mailto:mahmood@canaishopyou.com">mahmood@canaishopyou.com</a></div>
</div></body></html>"""

INDEX_PAGE = """<!doctype html><html><head><title>Independent AI-Commerce Testing | CanAIShopYou</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<meta property="og:title" content="Independent AI-Commerce Testing">
<meta property="og:description" content="We run controlled tests of whether AI shopping systems can find, read and buy from DTC stores. Findings are shared privately with each merchant.">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🔍</text></svg>">
<style>""" + BASE_CSS + """</style></head><body>
""" + NAV + """
<div class="hero"><h1>Independent <em>AI-commerce</em> testing</h1>
<p>We run controlled tests of whether AI shopping systems can discover, understand and buy from DTC stores.<br>
<b>Findings are shared privately with each merchant</b> — we don't publish scores, grades or rankings.</p></div>
<div class="card"><b>How it works</b><br><span style="color:var(--mut)">For each store we run realistic shopping scenarios and record where the journey succeeds or breaks — discovery, product information, selection and checkout. Every finding is reproduced and versioned, with documented limitations. Detailed observations go directly to the brand, with methodology available on request. We do not publish specific negative findings about any store.</span></div>
<div class="card cta"><h3>Want to know how AI shoppers do on your store?</h3>
<p>Run the free scan on your own store — or request a full independent test.</p>
<a class="btn" href="/">Scan your store free →</a></div>
<div class="foot">Independent AI-commerce testing · methodology available on request.<br>
<a href="mailto:mahmood@canaishopyou.com">mahmood@canaishopyou.com</a></div>
</div></body></html>"""

FOOT = ('<div class="foot">CanAIShopYou · independent AI-commerce testing · Multan, Pakistan<br>'
        '<a href="/about">About</a> · <a href="/privacy">Privacy</a> · <a href="/terms">Terms</a> · '
        '<a href="mailto:mahmood@canaishopyou.com">mahmood@canaishopyou.com</a></div>')

def static_page(title, desc, hero_h1, hero_sub, body):
    """A plain, INDEXABLE trust page (About/Privacy/Terms) in the site's own design system.
    These are deliberately not noindex'd — they're the legitimacy signals search engines and AI
    assistants look for when someone checks whether the business is real."""
    head = ("<!doctype html><html><head><title>" + title + " | CanAIShopYou</title>"
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="description" content="' + desc + '">'
            '<meta property="og:title" content="' + title + ' | CanAIShopYou">'
            '<meta property="og:description" content="' + desc + '">'
            '<link rel="icon" href="data:image/svg+xml,<svg xmlns=\'http://www.w3.org/2000/svg\''
            " viewBox='0 0 100 100'><text y='.9em' font-size='90'>🔍</text></svg>\">"
            "<style>" + BASE_CSS + "</style></head><body>")
    hero = ('<div class="hero" style="padding-bottom:20px"><h1>' + hero_h1 + '</h1>'
            + ('<p>' + hero_sub + '</p>' if hero_sub else '') + '</div>')
    return head + NAV + hero + '<div class="wrap">' + body + FOOT + '</div></body></html>'

ABOUT_BODY = """
<div class="card"><b>What CanAIShopYou Is</b><br><span style="color:var(--mut)">
CanAIShopYou is an independent commerce intelligence platform. We benchmark how AI assistants and autonomous
shopping agents—the systems consumers use to research and buy—discover, evaluate, and recommend online brands.
Our infrastructure monitors where the discovery pipeline breaks, reproduces findings across multiple controlled
simulation runs, and provides documented, actionable intelligence privately to merchants.</span></div>

<div class="card"><b>Our Foundation</b><br><span style="color:var(--mut)">
CanAIShopYou operates as an independent commerce intelligence lab dedicated to building a transparent testing
layer for AI search. Headquartered in Multan, Pakistan, we analyze AI retrieval models to support
direct-to-consumer (DTC) brands across the US and EU. For methodologies or custom inquiries, reach our research
team directly at <a href="mailto:mahmood@canaishopyou.com">mahmood@canaishopyou.com</a>.</span></div>

<div class="card"><b>How We Work</b><br><span style="color:var(--mut)">
We maintain absolute independence—we are not affiliated with, funded by, or endorsed by any brand we audit or
any AI provider. Every finding is rigorously reproduced and documented under specific environmental constraints
rather than broad generalizations. To protect brand integrity, we never publish negative findings publicly; all
vulnerability reports are routed directly and securely to the respective merchant.</span></div>

<div class="card"><b>Data &amp; Privacy Guardrails</b><br><span style="color:var(--mut)">
We operate on zero-trust data principles. We never request passwords, financial details, or credential
authorization. CanAIShopYou does not sell data, utilize advertising trackers, or store proprietary information.
Our automated scanner only parses publicly accessible pages of the storefronts submitted for analysis.</span></div>

<div class="card cta"><h3>Audit Your Brand's AI Visibility</h3>
<p>Run a free 30-second scan to evaluate your storefront, or contact us to schedule a comprehensive, full-scale
independent audit.</p>
<a class="btn" href="/">Run the free scan →</a></div>
"""

PRIVACY_BODY = """
<div class="card"><b>The short version</b><br><span style="color:var(--mut)">
We collect the minimum needed to run a test and send you the results. We don't sell your data, we don't share
it with advertisers, and we never ask for passwords, payment or wallet details.</span></div>

<div class="card"><b>What we collect</b><br><span style="color:var(--mut)">
• The store domain you submit to the scanner.<br>
• Your email address — only if you request a full audit or ask us to send findings.<br>
• Standard server logs (IP, timestamp) for security and reliability.</span></div>

<div class="card"><b>How we use it</b><br><span style="color:var(--mut)">
Only to run the requested test, deliver the results, and reply to you about them. We do not sell, rent or
trade your information, and we don't load third-party advertising trackers.</span></div>

<div class="card"><b>Access &amp; deletion</b><br><span style="color:var(--mut)">
Email <a href="mailto:mahmood@canaishopyou.com">mahmood@canaishopyou.com</a> any time to see what we hold
about you, or to have it deleted.</span></div>
"""

TERMS_BODY = """
<div class="card"><b>What the service is</b><br><span style="color:var(--mut)">
CanAIShopYou provides independent tests of how AI shopping systems handle online stores. Findings are
<b>observations under stated conditions</b> — a specific AI model, on a specific date, in a specific test
environment — not guarantees of how every AI system or every shopper will behave.</span></div>

<div class="card"><b>No warranty</b><br><span style="color:var(--mut)">
The scanner and reports are provided "as is". AI systems change constantly, so findings can change too. We
don't warrant that results are complete or error-free, and business decisions you make from them are your own.</span></div>

<div class="card"><b>Acceptable use</b><br><span style="color:var(--mut)">
Use the scanner only on stores you own or are authorised to test. Don't use the service to attempt to harm,
overload or misrepresent any third party.</span></div>

<div class="card"><b>Independence</b><br><span style="color:var(--mut)">
CanAIShopYou is not affiliated with, sponsored by or endorsed by any brand we test, or by any AI provider
(including OpenAI, Google or Anthropic). All product and company names are the property of their owners.</span></div>

<div class="card"><b>Contact</b><br><span style="color:var(--mut)">
Questions about these terms? <a href="mailto:mahmood@canaishopyou.com">mahmood@canaishopyou.com</a>.</span></div>
"""

app = Flask(__name__)

@app.route("/", methods=["GET"])
def index():
    # measurement era retired 1 Sep 2026 — the homepage is the connect funnel only
    return render_template_string(PAGE, r=None, ai=None, domain=None, scans=scan_count(), recent=recent_scans())

@app.route("/ai-test", methods=["GET", "POST"])
def ai_test():
    return redirect("/", code=301)   # retired with the measurement pivot
def _ai_test_retired():
    f = request.form
    domain = re.sub(r"^https?://", "", (f.get("domain", "")).strip().lower()).split("/")[0]
    email  = (f.get("email", "")).strip()
    niche  = (f.get("niche", "")).strip()
    rivals = [x.strip() for x in (f.get("rivals", "")).split(",") if x.strip()]
    ip = (request.headers.get("X-Forwarded-For", request.remote_addr or "")).split(",")[0].strip()
    if not (domain and email and niche):
        return render_template_string(BASE_DOC, body="<div class='wrap'><div class='card cta' style='margin-top:50px'><h3>One more thing</h3><p>Domain, email, and what you sell are all required to run the live test.</p><a class='btn' href='/'>← Back</a></div></div>")
    try: log_lead(domain, "", email, extra=f"category: {niche}", kind="ai-test")   # capture the lead no matter what
    except Exception: pass
    try:
        allowed, _why = _ai_allowed(ip)
        if _ai_key() and allowed:
            _ai_log_run(ip)
            res = run_ai_test(domain, niche, rivals)
            if res.get("ok"):
                return render_template_string(BASE_DOC, body=render_ai_result(res, domain, email))
    except Exception:
        import traceback, sys; traceback.print_exc(file=sys.stderr)  # -> Render logs, never a raw 500
    # fallback — no key / over cap / rate-limited / error: honest queue + lead captured
    return render_template_string(BASE_DOC, body=(
        "<div class='wrap'><div class='card cta' style='margin-top:50px'><h3>You're queued ✓</h3>"
        "<p>We'll run the full AI-visibility test on <b>" + _html.escape(domain) + "</b> and email the results"
        + ((" to <b>" + _html.escape(email) + "</b>") if email else "") + " within a day — with the exact prompts so you can verify every line.</p>"
        "<a class='btn' href='/'>← Back</a></div></div>"))

try:
    import feed_engine as _fe
except Exception:
    _fe = None
FEEDS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feeds")

@app.route("/connect", methods=["POST"])
def connect():
    f = request.form
    domain = re.sub(r"^https?://", "", (f.get("domain", "")).strip().lower()).split("/")[0]
    email = (f.get("email", "")).strip()
    if not (domain and email):
        return render_template_string(BASE_DOC, body="<div class='wrap'><div class='card cta' style='margin-top:50px'><h3>One more thing</h3><p>Store domain and email are both required.</p><a class='btn' href='/'>&larr; Back</a></div></div>")
    ip = (request.headers.get("X-Forwarded-For", request.remote_addr or "")).split(",")[0].strip()
    try:
        d = _load(); now = _utcnow()
        recent = [x for x in d.get("connect_runs", []) if x.get("ip") == ip and now - x["ts"] < 3600]
        if len(recent) >= 3:
            return render_template_string(BASE_DOC, body="<div class='wrap'><div class='card cta' style='margin-top:50px'><h3>One moment</h3><p>You've run a few checks this hour &mdash; email <a href='mailto:mahmood@canaishopyou.com' style='color:#fff'>mahmood@canaishopyou.com</a> and we'll take it from here.</p><a class='btn' href='/'>&larr; Back</a></div></div>")
        d.setdefault("connect_runs", []).append({"ip": ip, "ts": now}); d["connect_runs"] = d["connect_runs"][-2000:]; _save(d)
    except Exception: pass
    try: log_lead(domain, "", email, extra="", kind="connect")
    except Exception: pass
    rep = None
    if _fe:
        try: rep = _fe.run(domain, outdir=FEEDS_DIR)
        except Exception:
            import traceback, sys as _s; traceback.print_exc(file=_s.stderr)
    if not rep or not rep.get("ok"):
        reason = (rep or {}).get("reason", "we couldn't read a public catalog on this domain")
        body = ("<div class='wrap'><div class='card' style='margin-top:44px'><h2 style='margin:0 0 8px'>" + _html.escape(domain) + "</h2>"
                "<p style='color:var(--mut)'>No public catalog found &mdash; " + _html.escape(reason) + ".</p>"
                "<p>That usually means the store runs on <b>WooCommerce, BigCommerce or a custom stack</b> &mdash; exactly who we built this for. "
                "Connecting takes one step: your platform's read-only API keys, and we generate the feed from those.</p></div>"
                "<div class='card cta'><h3>We'll connect it with you</h3><p>Reply with your platform (Woo / BigCommerce / custom) and we'll onboard your catalog directly &mdash; you're in the queue as " + _html.escape(email) + ".</p>"
                "<a class='btn' href='mailto:mahmood@canaishopyou.com?subject=Connect%20" + _html.escape(domain) + "'>Connect my store &rarr;</a></div>"
                "<p style='text-align:center'><a href='/'>&larr; back</a></p></div>")
        return render_template_string(BASE_DOC, body=body)
    q = rep.get("data_quality", {})
    blockers = rep.get("checkout_blockers", [])
    fb = f"https://canaishopyou.com/feeds/{rep['domain']}.csv.gz"
    ck_ok = rep.get("checkout_eligible")
    rows = "".join(
        f"<tr><td style='padding:8px;border-bottom:1px solid var(--hair)'><b>{k}</b></td><td style='padding:8px;border-bottom:1px solid var(--hair)'>{v}</td></tr>"
        for k, v in [
            ("Platform", rep.get("platform", "?").title()),
            ("Products found", rep.get("products")),
            ("Feed rows generated", rep.get("feed_rows")),
            ("Currency", rep.get("currency") + ("" if rep.get("currency_detected") else " (assumed)")),
            ("Search eligibility", "<span class='PASS'>READY</span>"),
            ("Checkout eligibility", "<span class='PASS'>READY</span>" if ck_ok else "<span class='WARN'>BLOCKED</span> missing: " + ", ".join(blockers)),
            ("Items missing GTIN/SKU", q.get("no_identifier", 0)),
            ("Items missing images", q.get("no_image", 0)),
        ])
    body = ("<div class='wrap'><div class='card' style='margin-top:44px'>"
            "<div style='color:#0a7d3c;font-weight:700;font-size:12px'>YOUR FEED IS BUILT &middot; LIVE</div>"
            f"<h2 style='margin:.2em 0'>{_html.escape(rep['domain'])}</h2>"
            f"<p style='font-size:1.05em'>We just generated your <b>submission-ready OpenAI product feed</b> &mdash; {rep.get('feed_rows')} items, to spec, hosted and auto-refreshing:</p>"
            f"<p><code style='font-size:.9em'>{fb}</code></p>"
            f"<table style='width:100%;border-collapse:collapse;font-size:.92em'>{rows}</table></div>"
            "<div class='card cta'><h3>Next: we put it to work</h3>"
            "<p>Feed hosting + auto-refresh, crawler access, your ChatGPT merchant application handled &mdash; and agent checkout when your approval lands. That's the service.</p>"
            f"<a class='btn' href='mailto:mahmood@canaishopyou.com?subject=Launch%20{_html.escape(rep['domain'])}%20into%20AI%20shopping'>Launch my store &rarr;</a></div>"
            "<p style='text-align:center'><a href='/'>&larr; back</a></p></div>")
    return render_template_string(BASE_DOC, body=body)

@app.route("/feeds/<path:fname>")
def serve_feed(fname):
    from flask import send_from_directory, abort
    if not re.match(r"^[a-z0-9.-]+\.(tsv|csv\.gz)$", fname):
        abort(404)
    ua = request.headers.get("User-Agent", "")
    try:
        d = _load(); d.setdefault("feed_fetches", []).append({"f": fname, "ua": ua[:120], "ts": _utcnow()})
        d["feed_fetches"] = d["feed_fetches"][-2000:]; _save(d)
    except Exception: pass
    try:  # keep hosted feeds fresh: regenerate in the background if older than 6h
        import threading, time as _t
        fpath = os.path.join(FEEDS_DIR, fname)
        dom = fname.rsplit(".csv.gz", 1)[0].rsplit(".tsv", 1)[0]
        if _fe and os.path.exists(fpath) and _t.time() - os.path.getmtime(fpath) > int(os.environ.get("FEED_MAX_AGE", 21600)):
            os.utime(fpath, None)  # debounce concurrent fetches
            threading.Thread(target=lambda: _fe.run(dom, outdir=FEEDS_DIR), daemon=True).start()
    except Exception: pass
    return send_from_directory(FEEDS_DIR, fname)

@app.route("/index-report")
def index_report_legacy():
    # old path sounded like scanner-bait to trust checkers; permanent-redirect to the human name
    return redirect("/how-it-works", code=301)

@app.route("/robots.txt")
def robots_txt():
    # open to all crawlers incl. the AI search/user-fetch bots — we practice what we recommend
    return Response(
        "User-agent: *\nAllow: /\n\n"
        "User-agent: OAI-SearchBot\nAllow: /\n\n"
        "User-agent: ChatGPT-User\nAllow: /\n\n"
        "User-agent: Claude-SearchBot\nAllow: /\n\n"
        "User-agent: PerplexityBot\nAllow: /\n\n"
        "Sitemap: https://canaishopyou.com/sitemap.xml\n",
        mimetype="text/plain")

@app.route("/sitemap.xml")
def sitemap_xml():
    pages = ["", "how-it-works", "about", "privacy", "terms"]
    urls = "".join(f"<url><loc>https://canaishopyou.com/{p}</loc></url>" for p in pages)
    return Response('<?xml version="1.0" encoding="UTF-8"?>'
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    + urls + "</urlset>", mimetype="application/xml")

@app.route("/how-it-works")
def index_report():
    body = ("<div class='card'><b>1 &middot; Connect your store</b><br><span style='color:var(--mut)'>Shopify stores connect instantly from the domain. WooCommerce, BigCommerce and custom stacks connect with read-only API keys &mdash; no code on your side.</span></div>"
            "<div class='card'><b>2 &middot; We build and host your product feed</b><br><span style='color:var(--mut)'>Your catalog transformed to OpenAI's product-feed specification &mdash; every required field, checkout-eligibility flags, policy URLs &mdash; validated, hosted on our infrastructure and auto-refreshed so price and stock stay true.</span></div>"
            "<div class='card'><b>3 &middot; We handle the merchant application</b><br><span style='color:var(--mut)'>Crawler access, policies, and your ChatGPT merchant application prepared and submitted. We document every requirement and tell you exactly where it stands.</span></div>"
            "<div class='card'><b>4 &middot; Agent checkout</b><br><span style='color:var(--mut)'>Our hosted endpoint speaks the Agentic Commerce Protocol: ChatGPT completes the purchase, your payment account is charged, the order lands in your store. Rolling out to connected merchants.</span></div>"
            "<div class='card cta'><h3>See your eligibility in a minute</h3><p>Enter your store on the homepage &mdash; we generate your actual feed on the spot.</p><a class='btn' href='/'>Connect my store &rarr;</a></div>")
    return static_page("How it works", "How CanAIShopYou connects stores to ChatGPT Shopping: product feed, merchant application, agent checkout.",
                       "How <em>it works</em>", "From locked-out store to AI-shoppable, in four steps.", body)

@app.route("/about")
def about_page():
    body = ("<div class='card'><b>What we do</b><br><span style='color:var(--mut)'>People now shop &mdash; and check out &mdash; inside AI assistants. That channel runs on product feeds and agent-checkout protocols. Shopify and Etsy stores were integrated automatically; every other store was left out. CanAIShopYou is the on-ramp: we build, host and run the layer that makes independent stores findable and buyable by AI.</span></div>"
            "<div class='card'><b>Why it matters</b><br><span style='color:var(--mut)'>Which stores get to sell in the AI era shouldn't be decided by which platform they happened to build on. We exist so the non-Shopify half of commerce isn't locked out of the next channel.</span></div>"
            "<div class='card'><b>Who</b><br><span style='color:var(--mut)'>Built by Mahmood &mdash; a solo founder working in public. Questions, stores, partnerships: <a href='mailto:mahmood@canaishopyou.com'>mahmood@canaishopyou.com</a></span></div>")
    return static_page("About", "CanAIShopYou connects independent stores to AI shopping: product feeds, merchant onboarding and agent checkout.",
                       "About <em>CanAIShopYou</em>", "The on-ramp to AI shopping for stores outside Shopify.", body)

@app.route("/privacy")
def privacy_page():
    return static_page("Privacy", "What data CanAIShopYou collects and how it's used. We don't sell data or ask for passwords or payment.",
                       "Privacy", "The minimum we need, used only to run your test.", PRIVACY_BODY)

@app.route("/terms")
def terms_page():
    return static_page("Terms", "Terms of use for CanAIShopYou — findings are observations under stated conditions, provided independently.",
                       "Terms", "Plain terms for an independent testing service.", TERMS_BODY)

@app.route("/report/<path:domain>")
def brand_report_page(domain):
    return redirect("/", code=301)   # retired with the measurement pivot
def _brand_report_retired(domain):
    domain = re.sub(r"^https?://", "", domain.strip().lower()).split("/")[0].replace("www.", "")
    r = brand_report(domain)
    if not r:
        # not an Index brand — send them to the free scanner instead of scanning arbitrary input
        return render_template_string(BASE_DOC, body=(
            "<div class='wrap'><div class='card cta' style='margin-top:60px'>"
            "<h3>" + domain + " isn't on the Index yet</h3>"
            "<p>Run it through the free scanner to get its agent-readiness score.</p>"
            "<a class='btn' href='/'>Scan " + domain + " free →</a></div></div>")), 404
    class O(dict): __getattr__ = dict.get
    return render_template_string(REPORT_PAGE, r=O(r))


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

REPORT_PAGE = """<!doctype html><html><head><title>{{r.domain}} — Independent AI-Commerce Test | CanAIShopYou</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<meta name="description" content="{{r.domain}} was independently tested by CanAIShopYou for AI-commerce performance. Detailed findings are shared privately with the merchant.">
<meta property="og:title" content="{{r.domain}} — independently tested for AI commerce">
<meta property="og:description" content="CanAIShopYou independently tests whether AI shopping systems can find, read and buy from a store. Findings are shared privately with the merchant.">
<style>""" + BASE_CSS + """</style></head><body>""" + NAV + """
<div class="hero" style="padding-bottom:8px">
<h1><em>{{r.domain}}</em><br>independently tested</h1></div>
<div class="wrap">
<div class="repcard">
<div style="font-size:1.02em;color:var(--mut);font-weight:600">Last independently tested</div>
<div style="font-size:1.9em;font-weight:800;margin:6px 0;letter-spacing:-.5px">{{r.tested_date}}</div>
<div class="rank" style="margin-top:8px">Testing completed. Detailed findings are shared <b>privately with the merchant</b>.</div>
</div>
<div class="card"><b>What this is</b><br><span style="color:var(--mut)">CanAIShopYou runs independent, controlled tests of whether AI shopping systems can discover, understand and complete a purchase journey for a store. We do not publish specific findings — they go directly and privately to the brand. Methodology available on request.</span></div>

<div class="card cta" style="margin-top:30px">
<h3>On the {{r.domain}} team? Request your findings.</h3>
<p>We'll send the reproduced observations, the recorded evidence, and the methodology — free, no strings.</p>
<form class="scan" method="post" action="/request" style="max-width:520px">
<input type="hidden" name="domain" value="{{r.domain}}">
<input name="email" type="email" placeholder="you@{{r.domain}}" required>
<button type="submit">Send me the findings →</button></form>
</div>
<div class="foot">Independent AI-commerce testing · <a href="/">what we do</a> · <a href="/">scan your own store</a><br>
<a href="mailto:mahmood@canaishopyou.com">mahmood@canaishopyou.com</a></div>
</div></body></html>"""

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
