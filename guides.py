"""
guides.py — SEO guide pages for canaishopyou.com (Flask Blueprint).

Four long-form guides written from the steps we actually ran for a WooCommerce store on
3 September 2026 (Google Merchant Center, Microsoft Merchant Center, Perplexity, and an
overview checklist of every AI shopping surface a non-Shopify store can enter today).

Register from app.py, AFTER `app = Flask(__name__)`:

    from guides import bp as guides_bp, GUIDE_URLS; app.register_blueprint(guides_bp)

The site's design system (BASE_CSS / NAV / FOOT) is imported lazily inside the render
helper at request time, so importing this module never imports app.py (no circular import).

Claims discipline (binding): never claim inclusion or ranking; never "Instant Checkout";
never "we get you in". We prepare, validate and submit with the merchant; each platform decides.
"""
import json
import re

from flask import Blueprint

bp = Blueprint("guides", __name__)

SITE = "https://canaishopyou.com"
PUBLISHED = "2026-09-03"

GUIDE_URLS = [
    "/google-merchant-center-woocommerce",
    "/microsoft-merchant-center-copilot",
    "/perplexity-merchant-program",
    "/ai-shopping-surfaces-checklist",
]

# ----------------------------------------------------------------------------- rendering

GUIDE_CSS = """
.guide h2{font-size:1.28em;font-weight:750;letter-spacing:-.4px;margin:0 0 8px;color:var(--ink)}
.guide h3{font-size:1.02em;font-weight:700;margin:14px 0 4px;color:var(--ink)}
.guide p,.guide li{color:var(--mut);line-height:1.66;font-size:.97em;font-weight:400}
.guide p{margin:8px 0}
.guide ul,.guide ol{margin:8px 0 0;padding-left:22px}
.guide li{margin:6px 0}
.guide .lead p{color:var(--ink);font-size:1.06em}
.guide code{background:#f1f3f8;border-radius:6px;padding:2px 6px;font-size:.88em;color:var(--ink)}
.guide a{color:var(--accent);text-decoration:none;font-weight:550}
.guide a:hover{text-decoration:underline}
.guide .stepn{display:block;font-size:.7em;font-weight:700;letter-spacing:.8px;text-transform:uppercase;background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent;margin-bottom:6px}
.guide .tw{overflow-x:auto}
.guide table{font-size:.88em;min-width:720px}
.guide td{vertical-align:top;color:var(--mut);line-height:1.5}
.guide td b{color:var(--ink)}
.guide .related a{display:inline-block;margin:4px 14px 4px 0}
.guide .cta h3{font-size:1.6em;margin:0 0 12px;color:#fff}
.guide .cta p{color:rgba(255,255,255,.86)}
.guide .cta a{color:#fff;text-decoration:underline}
.guide .cta form.scan{margin:0 auto;max-width:600px;flex-wrap:wrap}
.guide .cta input[name=domain],.guide .cta input[type=email]{background:rgba(255,255,255,.16);border:1px solid rgba(255,255,255,.3);color:#fff}
.guide .cta input::placeholder{color:rgba(255,255,255,.7)}
.guide .cta .fine{font-size:.82em;color:rgba(255,255,255,.78);margin:12px 0 0}
"""

_TAG = re.compile(r"<[^>]+>")


def _text(html):
    """Plain text for JSON-LD: strip tags, unescape the few entities we use."""
    t = _TAG.sub("", html)
    for a, b in (("&mdash;", "—"), ("&ndash;", "–"), ("&rsquo;", "’"), ("&lsquo;", "‘"), ("&ldquo;", "“"),
                 ("&rdquo;", "”"), ("&rarr;", "→"), ("&larr;", "←"), ("&middot;", "·"), ("&le;", "≤"),
                 ("&ge;", "≥"), ("&amp;", "&"), ("&nbsp;", " ")):
        t = t.replace(a, b)
    return re.sub(r"\s+", " ", t).strip()


def _jsonld(path, headline, desc, faqs):
    url = SITE + path
    article = {
        "@context": "https://schema.org", "@type": "Article",
        "headline": headline, "description": desc, "url": url,
        "mainEntityOfPage": {"@type": "WebPage", "@id": url},
        "datePublished": PUBLISHED, "dateModified": PUBLISHED, "inLanguage": "en",
        "author": {"@type": "Organization", "name": "CanAIShopYou", "url": SITE},
        "publisher": {"@type": "Organization", "name": "CanAIShopYou", "url": SITE},
    }
    faq = {
        "@context": "https://schema.org", "@type": "FAQPage",
        "mainEntity": [{"@type": "Question", "name": _text(q),
                        "acceptedAnswer": {"@type": "Answer", "text": _text(a)}} for q, a in faqs],
    }
    crumbs = {
        "@context": "https://schema.org", "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "CanAIShopYou", "item": SITE + "/"},
            {"@type": "ListItem", "position": 2, "name": "Guides", "item": SITE + "/ai-shopping-surfaces-checklist"},
            {"@type": "ListItem", "position": 3, "name": headline, "item": url},
        ],
    }
    return "".join('<script type="application/ld+json">' + json.dumps(x, ensure_ascii=False) + "</script>"
                   for x in (article, faq, crumbs))


def _faq_html(faqs):
    return ('<div class="card faq"><h2>Frequently asked</h2>'
            + "".join("<h3>" + q + "</h3><p>" + a + "</p>" for q, a in faqs) + "</div>")


def _cta(h3, p):
    """The site's connect-form CTA: same markup the homepage uses (POST /connect, domain + email)."""
    return ('<div class="card cta"><h3>' + h3 + "</h3><p>" + p + "</p>"
            '<form class="scan" method="post" action="/connect">'
            '<input name="domain" placeholder="yourstore.com" required>'
            '<input name="email" type="email" placeholder="you@yourstore.com" required>'
            "<button>Connect my store &rarr;</button></form>"
            '<p class="fine">Free eligibility check &mdash; your feeds are generated on the spot. '
            'Want it done for you? <a href="/">Launch and Hosting pricing is on the homepage</a> &middot; '
            '<a href="/how-it-works">how it works</a>.</p></div>')


def _related(*links):
    return ('<div class="card related"><h2>Related guides</h2>'
            + "".join('<a href="' + href + '">' + label + " &rarr;</a>" for href, label in links) + "</div>")


def _render(path, title, desc, h1, sub, body, faqs):
    from app import BASE_CSS, NAV, FOOT  # lazy: app.py imports this module after `app` exists
    url = SITE + path
    head = ("<!doctype html><html lang=\"en\"><head><title>" + title + " | CanAIShopYou</title>"
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="description" content="' + desc + '">'
            '<link rel="canonical" href="' + url + '">'
            '<meta property="og:type" content="article">'
            '<meta property="og:title" content="' + title + ' | CanAIShopYou">'
            '<meta property="og:description" content="' + desc + '">'
            '<meta property="og:url" content="' + url + '">'
            '<link rel="icon" href="data:image/svg+xml,<svg xmlns=\'http://www.w3.org/2000/svg\''
            " viewBox='0 0 100 100'><text y='.9em' font-size='90'>🔍</text></svg>\">"
            + _jsonld(path, _text(title), desc, faqs)
            + "<style>" + BASE_CSS + GUIDE_CSS + "</style></head><body>")
    hero = ('<div class="hero" style="padding-bottom:20px"><h1>' + h1 + "</h1><p>" + sub + "</p></div>")
    return head + NAV + hero + '<div class="wrap guide">' + body + _faq_html(faqs) + FOOT + "</div></body></html>"


# ----------------------------------------------------------------------------- 1. Google Merchant Center

@bp.route("/google-merchant-center-woocommerce")
def guide_google_merchant_center():
    faqs = [
        ("Do I need a Google Ads account or a credit card?",
         "No. Free product listings run from Merchant Center alone. We created the account, added the feed, set delivery and returns, and reached &ldquo;Your products can now show on Google&rdquo; without a Google Ads account and without entering a card. Ads are a separate, optional step."),
        ("How long until products are live?",
         "After the feed is fetched, every product sits &ldquo;Under review&rdquo;. Google says review can take up to about three business days. Individual products can still be disapproved afterwards (price mismatch, missing shipping for a target country, policy problems), so check the Products page after a few days."),
        ("Which feed format does Merchant Center take from a WooCommerce store?",
         "A tab- or comma-separated file in Google&rsquo;s product data specification, reachable at a public URL. Merchant Center fetches it on a schedule (every 24 hours) with no authentication. Google&rsquo;s own WooCommerce extension can sync products too; a hosted feed URL works regardless of plugins, and the same file is reusable for Microsoft, Meta and Perplexity."),
        ("Does Google Merchant Center put my products in ChatGPT?",
         "No. Google&rsquo;s surfaces are Google Shopping, AI Mode, AI Overviews and Gemini. ChatGPT is a separate route (a merchant application on a waitlist, or Shopify Catalog). See our <a href=\"/get-into-chatgpt-shopping\">ChatGPT Shopping guide</a>. The Bing index does matter for ChatGPT search, which is why we do <a href=\"/microsoft-merchant-center-copilot\">Microsoft Merchant Center</a> next."),
        ("Can I list in more countries than I ship to?",
         "You can, but you shouldn&rsquo;t. Every target country needs a matching delivery configuration; products for a country with no delivery settings get disapproved. Pick only the countries you actually ship to and add more later, with their own rates."),
    ]
    body = (
        '<div class="card lead"><p>Google&rsquo;s free product listings put a store&rsquo;s catalog in the Shopping tab, and Google has said its AI shopping features &mdash; AI Mode, AI Overviews and Gemini &mdash; are built on the same Shopping Graph that Merchant Center feeds. '
        'Shopify stores get a one-click Google channel. WooCommerce stores set this up themselves, and the interface asks questions that are easy to answer wrong. '
        'This is the exact sequence we ran for a WooCommerce store on 3 September 2026, with the settings we chose and why. Budget about an hour. No Google Ads account, no card.</p></div>'

        '<div class="card"><h2>Before you start</h2><ul>'
        '<li><b>A live store with real product pages</b> &mdash; price, stock and images visible without a login. Merchant Center checks the landing page against the feed.</li>'
        '<li><b>Shipping and returns pages published</b> on the store. Google reads the returns page you link; the returns option you choose in Merchant Center has to match what it says.</li>'
        '<li><b>Your legal business address.</b> Merchant Center asks for the registered address of the legal entity behind the store (for us, a Delaware LLC), not the warehouse or the founder&rsquo;s home.</li>'
        '<li><b>A Google-format product feed at a public URL.</b> The free <a href="/">connect form</a> builds one from a WooCommerce or Shopify store URL, keyless, and hosts it at <code>/feeds/&lt;your-domain&gt;.google.tsv</code> with a daily refresh.</li>'
        '<li><b>Access to your DNS</b> for Search Console verification. Ours lived at Hostinger (the host), not at the registrar.</li>'
        '</ul></div>'

        '<div class="card"><span class="stepn">Step 1 &middot; about 15 minutes</span><h2>Verify the domain in Google Search Console</h2>'
        '<p>Search Console proves you own the domain, gets product pages crawled, and lets Bing Webmaster Tools import everything in one click later.</p><ol>'
        '<li>Add a <b>Domain</b> property (not a URL-prefix property) for your bare domain.</li>'
        '<li>Google gives you a TXT record. Add it at whoever hosts your DNS. If a managed WordPress host runs your nameservers, the record goes in their panel, not the registrar&rsquo;s.</li>'
        '<li>Click Verify. If it fails, wait a few minutes and retry rather than re-adding the record.</li>'
        '<li>Open Sitemaps and submit the full URL: <code>https://yourstore.com/sitemap.xml</code>. For a Domain property you paste the complete URL, not a relative path.</li>'
        '</ol><p>On WooCommerce with the Slim SEO plugin, <code>/sitemap.xml</code> is generated automatically and includes a product sitemap. Yoast, Rank Math and WordPress core produce the equivalent; use whichever your site exposes.</p></div>'

        '<div class="card"><span class="stepn">Step 2 &middot; about 10 minutes</span><h2>Create the Merchant Center account</h2><ol>'
        '<li>Open <code>merchants.google.com</code> signed in to the Google account that should own the store. If you have never had an account, the page says your account doesn&rsquo;t have access &mdash; that is expected. Click <b>Sign up for Merchant Center</b>.</li>'
        '<li>Business name: the store name shoppers see (ours was the brand, not the LLC).</li>'
        '<li><b>Do you sell online?</b> Yes. <b>Do you have a physical shop?</b> No, for a pure online store.</li>'
        '<li><b>Registered business address:</b> the legal entity&rsquo;s registered address. This is also what Microsoft will later compare against, so use the same address everywhere.</li>'
        '<li><b>Countries:</b> select only the countries you ship to. We chose United States only. Adding a country you have no delivery rates for gets its products disapproved rather than listed.</li>'
        '</ol></div>'

        '<div class="card"><span class="stepn">Step 3 &middot; about 5 minutes</span><h2>Add products from a scheduled feed URL</h2><ol>'
        '<li>In Products, choose <b>Add products from a file</b>, then <b>Enter a link to your file</b>.</li>'
        '<li>Paste the public URL of your Google-format feed (TSV or CSV). Merchant Center fetches it on a schedule, every 24 hours, with no username or password. Keep the URL stable; if it changes, edit the source rather than adding a second one.</li>'
        '<li>Give the feed a name and save. The first fetch runs immediately; later ones follow the schedule.</li>'
        '</ol><p>What the file needs: Google&rsquo;s product data specification &mdash; at minimum a stable <code>id</code>, <code>title</code>, <code>description</code>, <code>link</code>, <code>image_link</code>, <code>price</code> with currency, <code>availability</code>, and <code>brand</code> plus identifier data (GTIN/MPN, or a flag that none exists) where Google requires it. One row per variant. '
        'If the feed you have was built for OpenAI&rsquo;s spec, it is not the same file; the field names differ. Our connect form writes both from one catalog read.</p></div>'

        '<div class="card"><span class="stepn">Step 4 &middot; about 10 minutes</span><h2>Delivery settings: enter times manually</h2>'
        '<p>Merchant Center offers to estimate delivery from carrier data. For a store that makes to order (print-on-demand, custom goods, small batches) that estimate ignores production time, and a promised date you can&rsquo;t meet is a fast way to lose a listing. Choose <b>Enter specific delivery times manually</b> and set real numbers. Ours:</p><ul>'
        '<li><b>Order cut-off time:</b> 14:00, time zone Eastern (match the time zone your fulfilment actually works in).</li>'
        '<li><b>Handling time:</b> 2&ndash;5 business days, Monday to Friday.</li>'
        '<li><b>Transit time:</b> 3&ndash;5 days.</li>'
        '<li><b>Delivery cost:</b> flat rate $6.90, with free delivery on orders over $75.</li>'
        '</ul><p>Use your own rates, but keep them identical to what the store charges at checkout. Merchant Center compares the two and a mismatch is a disapproval reason.</p></div>'

        '<div class="card"><span class="stepn">Step 5 &middot; about 5 minutes</span><h2>Returns: link the policy and pick the matching option</h2>'
        '<p>Paste the URL of your returns page, then choose the returns statement that describes it. We selected <b>&ldquo;I accept returns for defective products only&rdquo;</b> because that is what our returns page says. Google checks the linked page against the option, so if your page offers 30-day returns for any reason, choose that instead &mdash; do not pick the option you wish were true. '
        'The returns window in your feed (if you include one) should agree with the page as well.</p></div>'

        '<div class="card"><span class="stepn">Step 6</span><h2>What you see when it works</h2>'
        '<p>With onboarding complete, Merchant Center shows <b>&ldquo;Your products can now show on Google&rdquo;</b>. That is the account being ready, not the products: every item enters <b>Under review</b>, which Google says can take up to about three business days. Come back after that and read the Products view; items with problems list the reason and the fix.</p>'
        '<p>Nothing here requires Google Ads. Free listings are the default; you can add a campaign later if you want paid placement.</p></div>'

        '<div class="card"><h2>Mistakes we would avoid a second time</h2><ul>'
        '<li><b>Looking for the TXT record at the registrar</b> when a hosting company manages the nameservers.</li>'
        '<li><b>Ticking extra target countries.</b> Each needs its own delivery configuration or its products are disapproved.</li>'
        '<li><b>A returns option that doesn&rsquo;t match the returns page.</b> Fix one until they agree.</li>'
        '<li><b>A feed behind a login or a changing URL.</b> The scheduled fetch is unauthenticated and needs the same URL every day.</li>'
        '</ul></div>'

        '<div class="card"><h2>Same feed, next surfaces</h2>'
        '<p>The Google-format file you just connected is accepted, in the same shape, by Microsoft Merchant Center, Meta Commerce Manager and Perplexity&rsquo;s merchant program &mdash; and Microsoft can import your Google Merchant Center store directly, so the next twenty minutes are the highest-return work of the day: <a href="/microsoft-merchant-center-copilot">Microsoft Merchant Center in 20 minutes</a>. '
        'The full map is in the <a href="/ai-shopping-surfaces-checklist">one-day AI shopping surfaces checklist</a>.</p></div>'

        + _cta("Get your Google-format feed in a minute, free",
               "Enter your WooCommerce or Shopify store. We read the public catalog, build the Google-format feed plus the OpenAI-spec feed and a Shopify import CSV, validate them, and host them with a daily refresh. Then paste the URL into Merchant Center as above. Each platform decides inclusion; we make sure your data is ready and correct.")
        + _related(("/microsoft-merchant-center-copilot", "Microsoft Merchant Center and Copilot"),
                   ("/perplexity-merchant-program", "Perplexity Merchant Program"),
                   ("/ai-shopping-surfaces-checklist", "One-day AI surfaces checklist"),
                   ("/get-into-chatgpt-shopping", "Get into ChatGPT Shopping"),
                   ("/openai-product-feed-spec", "OpenAI product feed spec"),
                   ("/how-it-works", "How CanAIShopYou works"))
    )
    return _render(
        "/google-merchant-center-woocommerce",
        "Google Merchant Center for WooCommerce: free listings in Google Shopping, Gemini and AI Mode",
        "Google Merchant Center for a WooCommerce store, step by step: Search Console, a scheduled feed URL, delivery and returns settings, review. Free listings, no Google Ads.",
        "Get a WooCommerce store into <em>Google Shopping, Gemini and AI Mode</em> &mdash; free listings, step by step",
        "The exact sequence we ran for a WooCommerce store on 3 September 2026: Search Console, a Google-format feed, Merchant Center, delivery and returns. About an hour. No Google Ads account, no card.",
        body, faqs)


# ----------------------------------------------------------------------------- 2. Microsoft Merchant Center

@bp.route("/microsoft-merchant-center-copilot")
def guide_microsoft_merchant_center():
    faqs = [
        ("Do I have to run Bing ads?",
         "No. Choose &ldquo;Create an account only&rdquo; when Microsoft Advertising offers to build a campaign, and &ldquo;Set up payment later&rdquo; when it asks for a card. Free product listings in Bing Shopping and Copilot do not require a payment method. We finished with no card on file."),
        ("Do I need Google Merchant Center first?",
         "Not strictly &mdash; Microsoft Merchant Center also accepts a catalog feed file directly &mdash; but the import from Google Merchant Center is a few clicks, keeps one feed as the source of truth, and re-syncs daily. If you have not set up Google yet, do <a href=\"/google-merchant-center-woocommerce\">that guide</a> first; it takes about an hour."),
        ("Does this get my products into ChatGPT?",
         "Not directly. Merchant Center feeds Bing Shopping and Copilot&rsquo;s shopping answers. Separately, ChatGPT&rsquo;s web search relies on Bing&rsquo;s index, which is why the Bing Webmaster Tools step (sitemap, URL submission, IndexNow) matters: it makes your product pages findable to the search that ChatGPT uses. Discovery in ChatGPT Shopping itself is a different route; see the <a href=\"/get-into-chatgpt-shopping\">ChatGPT Shopping guide</a>."),
        ("I sell from outside the US. What location do I pick?",
         "The location of the business the store belongs to, and be sure before you click: it cannot be changed once the account exists. Our store is a US LLC shipping to the US, so United States was the only correct answer. If your entity and your shipping country differ, use the entity&rsquo;s country and set target markets in the store settings."),
        ("How long does store approval take?",
         "Microsoft&rsquo;s message on creation was that the store is processed for approval within up to three business days. Products appear in Bing Shopping and Copilot after the store is approved and the imported products pass review."),
    ]
    body = (
        '<div class="card lead"><p>Microsoft&rsquo;s shopping surfaces &mdash; Bing Shopping and the shopping answers inside Copilot &mdash; take product data through Microsoft Merchant Center, and the Bing index underneath them is also what ChatGPT&rsquo;s web search relies on. '
        'Shopify has a built-in Microsoft channel; every other store does this by hand. The good news: if Google Merchant Center is already set up, Microsoft can import the whole store, and the surrounding accounts take one click each. '
        'This is what we did for a WooCommerce store on 3 September 2026. Twenty minutes if Google is done; no card.</p></div>'

        '<div class="card"><span class="stepn">Step 1 &middot; about 5 minutes</span><h2>Bing Webmaster Tools: import from Google Search Console</h2><ol>'
        '<li>Sign in to Bing Webmaster Tools with the <b>same Google account</b> you used for Search Console.</li>'
        '<li>Choose <b>Import from Google Search Console</b>. Grant access, tick your site, import. Verification happens automatically &mdash; no DNS record, no meta tag &mdash; and the sitemap comes across with it.</li>'
        '<li>Then go to <b>Settings &rarr; API access</b> and generate an API key. The URL Submission API lets you push a batch of product and page URLs for crawling instead of waiting; the quota is 100 URLs a day and 2,800 a month. We used it to submit every product and policy page the same afternoon.</li>'
        '</ol><p>If your store runs WordPress, Microsoft&rsquo;s IndexNow plugin does the ongoing version of this: every time a product or page is saved, its URL is pinged to Bing. We installed it and re-saved all products and pages so each one was submitted once.</p>'
        '<p>Why bother with any of this before Merchant Center? Because ChatGPT search draws on Bing&rsquo;s index. A product page Bing has not crawled is invisible to that search, however good the feed.</p></div>'

        '<div class="card"><span class="stepn">Step 2 &middot; about 8 minutes</span><h2>Create the Microsoft Advertising account (without advertising)</h2>'
        '<p>Merchant Center lives inside Microsoft Advertising, so an advertising account has to exist even if you never run an ad. Go to <code>ads.microsoft.com</code> and sign up. Three settings deserve care:</p><ul>'
        '<li><b>Location.</b> Must match the business. For a US store, United States. It <b>cannot be changed later</b>; a wrong answer means starting over with a new account.</li>'
        '<li><b>Phone number.</b> Required. Use one you can answer; Microsoft may verify.</li>'
        '<li><b>Business name and address.</b> The <b>legal</b> business name and registered address &mdash; the same ones you gave Google. A mismatch between platforms, or between the name here and the entity that owns the domain, can block the account during review.</li>'
        '</ul><p>When the flow offers to build a first campaign, choose <b>Create an account only</b>. When it asks for billing, choose <b>Set up payment later</b>. Free listings never require the card; you can add one if you decide to advertise.</p></div>'

        '<div class="card"><span class="stepn">Step 3 &middot; about 5 minutes</span><h2>Merchant Center: import the store from Google</h2><ol>'
        '<li>In Microsoft Advertising open <b>Tools &rarr; Merchant Center</b> and click <b>Create store</b>.</li>'
        '<li>Choose <b>Import an existing store from Google Merchant Center</b>. A Google sign-in window opens. <b>Safari blocks this pop-up by default</b> and the button appears to do nothing; allow pop-ups for <code>ads.microsoft.com</code> in Safari&rsquo;s website settings and click again. Chrome usually shows it.</li>'
        '<li>Sign in with the Google account that owns Merchant Center and tick the Merchant Center account to import.</li>'
        '<li>Review the store name and description Microsoft pulled across, set the contact email, and click <b>Create store</b>. The confirmation says the store is processed for approval, up to three business days.</li>'
        '</ol></div>'

        '<div class="card"><span class="stepn">Step 4 &middot; about 2 minutes</span><h2>Run the first import and leave the schedule on</h2>'
        '<p>Open the new store, go to <b>Import</b>, and click <b>Import now</b> so the products arrive without waiting for the schedule. Then confirm the daily import from Google Merchant Center is scheduled &mdash; ours runs at 12 AM Pacific. From here, any product, price or stock change flows store &rarr; Google feed &rarr; Google Merchant Center &rarr; Microsoft with no further work. One feed, two platforms.</p>'
        '<p>Once the store is approved and products pass review, they are eligible to appear in Bing Shopping and in Copilot&rsquo;s shopping results. Approval and placement are Microsoft&rsquo;s decisions; the setup above is everything the merchant controls.</p></div>'

        '<div class="card"><h2>What tripped us up</h2><ul>'
        '<li><b>The pop-up.</b> On Safari the Google sign-in for the import silently fails until pop-ups are allowed for ads.microsoft.com.</li>'
        '<li><b>The location field.</b> It is permanent. Read it twice.</li>'
        '<li><b>Legal name versus brand name.</b> Microsoft wants the entity; the brand goes in the store name inside Merchant Center.</li>'
        '<li><b>Campaign and billing prompts.</b> They look mandatory. They are not: account only, payment later.</li>'
        '<li><b>Doing Merchant Center before Webmaster Tools.</b> Both are quick, but Webmaster Tools is the one that affects ChatGPT search, so do it first while you are signed into the Google account.</li>'
        '</ul></div>'

        '<div class="card"><h2>Where this sits in the bigger picture</h2>'
        '<p>Microsoft is the second of the self-serve, no-cost surfaces a non-Shopify store can enter in a day. Google is first (same feed, more setup), <a href="/perplexity-merchant-program">Perplexity</a> is a five-step form, and Meta Commerce Manager takes the same Google-format feed if you have a Facebook business login. ChatGPT is different: a waitlisted merchant application, or Shopify Catalog through Shopify&rsquo;s free Agentic plan. '
        'The <a href="/ai-shopping-surfaces-checklist">one-day checklist</a> lays them side by side with what each platform gates and what only the merchant can do.</p></div>'

        + _cta("Build the feed Microsoft and Google both accept, free",
               "Enter your store and we generate the Google-format feed (the one Google Merchant Center fetches and Microsoft imports), the OpenAI-spec feed and a Shopify import CSV from your public catalog, keyless, hosted with a daily refresh. You keep every account; each platform decides inclusion.")
        + _related(("/google-merchant-center-woocommerce", "Google Merchant Center for WooCommerce"),
                   ("/perplexity-merchant-program", "Perplexity Merchant Program"),
                   ("/ai-shopping-surfaces-checklist", "One-day AI surfaces checklist"),
                   ("/get-into-chatgpt-shopping", "Get into ChatGPT Shopping"),
                   ("/agentic-commerce-non-shopify", "Agentic commerce for non-Shopify stores"),
                   ("/how-it-works", "How CanAIShopYou works"))
    )
    return _render(
        "/microsoft-merchant-center-copilot",
        "Bing Shopping and Copilot for non-Shopify stores: Microsoft Merchant Center in 20 minutes",
        "Bing Shopping and Copilot for a non-Shopify store: Bing Webmaster Tools import, a Microsoft Advertising account with no card, Merchant Center store imported from Google, daily sync.",
        "Bing Shopping and Copilot for <em>non-Shopify stores</em>: Microsoft Merchant Center in 20 minutes",
        "Bing Webmaster Tools in one click, an advertising account with no card, and a Merchant Center store imported straight from Google. What we did on 3 September 2026, including the setting you cannot change afterwards.",
        body, faqs)


# ----------------------------------------------------------------------------- 3. Perplexity Merchant Program

@bp.route("/perplexity-merchant-program")
def guide_perplexity_merchant_program():
    faqs = [
        ("What does the Perplexity Merchant Program cost?",
         "The program is free to join and charges no commission on sales; that is stated in the application itself. Shoppers who find a product in Perplexity click through to your own store. Nothing in the form asks for a card."),
        ("Can a store outside the United States apply?",
         "The form requires that you sell and ship to the United States. A non-US company that ships to US customers meets that; a store that does not ship to the US does not qualify today."),
        ("How long does review take?",
         "Perplexity does not publish a review time and does not confirm receipt in the form beyond the submission itself. Reports from merchants describe waits measured in weeks rather than days. Treat it as a queue you join once and do not depend on, and put the same hour into Google and Microsoft, which approve within days."),
        ("Do I need a product feed to apply?",
         "Not to submit the form: the five steps ask about the business, the contact, the website, the vertical and order volume, not for a file. Have a Google-format feed ready anyway, because that is the standard catalog format across shopping surfaces and it is what you will be able to offer if Perplexity follows up."),
        ("Does applying guarantee my products show up in Perplexity?",
         "No. Applying puts your store in front of Perplexity&rsquo;s review; what appears in answers, and when, is Perplexity&rsquo;s decision. Independently of the program, keep your product pages crawlable by PerplexityBot so the assistant can cite them from the open web."),
    ]
    body = (
        '<div class="card lead"><p>Perplexity answers shopping questions with product cards and links, and it runs a merchant program that lets stores supply their catalog directly rather than hoping the crawler finds the right page. '
        'The program is free, takes no commission, and the application is a five-step form linked from the footer of perplexity.ai. It is also the least transparent of the self-serve surfaces: no published review time, no feed upload in the form. '
        'Here is what it is, exactly what the form asks, what to have ready, and where it belongs in a non-Shopify store&rsquo;s day of setup.</p></div>'

        '<div class="card"><h2>What the program is</h2>'
        '<p>Perplexity&rsquo;s merchant program is an application route for stores that sell and ship to the United States. Accepted merchants can provide product data to Perplexity so that shopping answers draw on the merchant&rsquo;s own catalog &mdash; titles, prices, availability, images &mdash; instead of whatever the crawler last saw. '
        'Purchases happen on the merchant&rsquo;s own site; Perplexity is a discovery surface here, not a checkout, and the program states it charges no commission.</p>'
        '<p>What it is not: it is not a self-serve console like Google or Microsoft Merchant Center where you paste a feed URL and watch products go under review. You apply, and Perplexity decides whether and when to follow up. Set expectations accordingly.</p></div>'

        '<div class="card"><span class="stepn">Where to find it</span><h2>The application is in the site footer</h2>'
        '<p>Go to <code>perplexity.ai</code>, scroll to the footer and look for <b>Merchant Program</b>. It is not in the main navigation and does not appear in the app. The link opens the application form. You do not need a Perplexity Pro subscription to apply.</p></div>'

        '<div class="card"><span class="stepn">About 5 minutes</span><h2>The five steps, and how we answered them</h2><ol>'
        '<li><b>Business name and legal entity.</b> Give the store&rsquo;s brand name and the legal entity that owns it (for us, a Delaware LLC). Use the same legal name and address you gave Google and Microsoft; consistency across platforms is cheap insurance.</li>'
        '<li><b>Contact.</b> A named person and an email at the store&rsquo;s domain. A generic free-mail address is a weaker signal that the applicant controls the site.</li>'
        '<li><b>Website.</b> The store URL. Make sure product pages, shipping, returns, privacy and terms are all published and reachable before you submit; reviewers will look.</li>'
        '<li><b>Vertical.</b> The category you sell in. Pick the closest single category rather than something broad.</li>'
        '<li><b>Estimated monthly orders.</b> An honest number. There is no published threshold, and a new store that overstates volume gains nothing if the store visibly has no reviews or sales history.</li>'
        '</ol><p>The form requires that you sell and ship to the United States. That is the only hard eligibility gate we saw. Submit, and note the date; there is no dashboard to check status afterwards.</p></div>'

        '<div class="card"><h2>What feed to have ready</h2>'
        '<p>The application does not take a file. What it can lead to is a request for your catalog, and across every shopping surface the common currency is the <b>Google Shopping product feed format</b>: one row per variant with <code>id</code>, <code>title</code>, <code>description</code>, <code>link</code>, <code>image_link</code>, <code>price</code>, <code>availability</code>, <code>brand</code> and identifiers. '
        'It is the file Google Merchant Center fetches, Microsoft imports, and Meta Commerce Manager schedules. Having it hosted at a stable public URL means you can answer a follow-up from Perplexity in one line instead of starting a project.</p>'
        '<p>The free <a href="/">connect form</a> builds that file from a WooCommerce or Shopify store URL, keyless, alongside the OpenAI-spec feed and a Shopify import CSV, and hosts it at <code>/feeds/&lt;your-domain&gt;.google.tsv</code> with a daily refresh.</p></div>'

        '<div class="card"><h2>Do this too: let PerplexityBot read your store</h2>'
        '<p>Independently of the program, Perplexity cites pages from the open web, and it crawls with a user agent called <code>PerplexityBot</code>. Check your <code>robots.txt</code> does not block it (many &ldquo;block all AI bots&rdquo; rules do), confirm product pages return HTTP 200 without a login, and keep a sitemap published. '
        'For a WordPress store the sitemap at <code>/sitemap.xml</code> from your SEO plugin is enough. This is the part you control today, while the application waits.</p></div>'

        '<div class="card"><h2>Where it belongs in your day</h2>'
        '<p>Perplexity is a five-minute task with an unknown wait, so it goes after the two surfaces that approve within days. Our order for a non-Shopify store: <a href="/google-merchant-center-woocommerce">Google Merchant Center</a> (about an hour, up to three business days review), then <a href="/microsoft-merchant-center-copilot">Microsoft Merchant Center</a> (twenty minutes, imports from Google), then this form, then Meta Commerce Manager if you have a Facebook business login. '
        'ChatGPT is its own track &mdash; feed-ready, application filed at chatgpt.com/merchants, and the Shopify Catalog route &mdash; covered in the <a href="/get-into-chatgpt-shopping">ChatGPT Shopping guide</a>. The <a href="/ai-shopping-surfaces-checklist">one-day checklist</a> puts all of them on one table.</p></div>'

        + _cta("Have your catalog ready before Perplexity asks",
               "Enter your store and we generate the Google-format feed, the OpenAI-spec feed and a Shopify import CSV from your public catalog in about a minute, keyless, hosted with a daily refresh. We prepare, validate and submit with you; each platform decides inclusion.")
        + _related(("/ai-shopping-surfaces-checklist", "One-day AI surfaces checklist"),
                   ("/google-merchant-center-woocommerce", "Google Merchant Center for WooCommerce"),
                   ("/microsoft-merchant-center-copilot", "Microsoft Merchant Center and Copilot"),
                   ("/get-into-chatgpt-shopping", "Get into ChatGPT Shopping"),
                   ("/openai-product-feed-spec", "OpenAI product feed spec"),
                   ("/how-it-works", "How CanAIShopYou works"))
    )
    return _render(
        "/perplexity-merchant-program",
        "Perplexity Merchant Program: what it is, how to apply, what feed it takes",
        "Perplexity's merchant program for online stores: where the application is, the five steps it asks, the US shipping rule, cost (free, no commission) and what feed to have ready.",
        "Perplexity Merchant Program: <em>what it is, how to apply</em>, what feed it takes",
        "A five-step form in the perplexity.ai footer, free and commission-free, for stores that ship to the US. What it asks, what to prepare, and what to expect afterwards.",
        body, faqs)


# ----------------------------------------------------------------------------- 4. One-day checklist (overview)

@bp.route("/ai-shopping-surfaces-checklist")
def guide_ai_surfaces_checklist():
    faqs = [
        ("How much does entering all of these cost?",
         "Google, Microsoft, Perplexity and Meta free listings cost nothing and need no card. Shopify&rsquo;s Agentic plan for Shopify Catalog is free. The OpenAI merchant application is free. The only spend is your time, roughly ninety minutes of account creation, and optionally a service like ours to build and host the feeds and do the setup with you."),
        ("Which of these actually put products in ChatGPT?",
         "Two routes exist. Shopify Catalog: OpenAI documents Shopify&rsquo;s catalog as integrated, and Shopify&rsquo;s free Agentic plan lets a non-Shopify store import its catalog while checkout stays on its own site; Shopify checks for a track record of genuine sales. The OpenAI merchant application at chatgpt.com/merchants: free to file, reviewed from a waitlist, with approved merchants pushing a feed by SFTP or API; small merchants report long silences. Bing indexing helps ChatGPT&rsquo;s web search find your pages regardless. None of these is inclusion by right; each platform decides."),
        ("Can I do this without a legal entity?",
         "Google and Microsoft both ask for a registered business address and Microsoft wants the legal business name; Perplexity asks for the legal entity. A sole proprietor can enter their own name and address, but the answers must be consistent across platforms and match what the store&rsquo;s pages say."),
        ("Do I need one feed per platform?",
         "Two. The Google Shopping format covers Google, Microsoft, Meta and Perplexity. OpenAI has its own field set (a different file) for the merchant application, and Shopify Catalog wants a Shopify product-import CSV. Our connect form writes all three from one read of your store."),
        ("What can a service do versus what only I can do?",
         "A service can read your catalog, build and host the feeds, validate them against each spec, draft every application, and sit with you through setup. Only you can own the accounts, supply the legal entity and phone number, accept each platform&rsquo;s terms, and be the applicant of record. The table above marks the split for every surface."),
    ]
    row = lambda cells: "<tr>" + "".join("<td>" + c + "</td>" for c in cells) + "</tr>"
    table = (
        '<div class="tw"><table><tr><th>Surface</th><th>Gate</th><th>Cost</th><th>Time to set up / review</th><th>What we can do</th><th>What only the merchant can do</th></tr>'
        + row(["<b>Google Shopping, AI Mode, Gemini</b><br>Google Merchant Center free listings",
               "Google account, registered business address, target countries with delivery settings, returns policy that matches your page, Google-format feed URL",
               "Free; no Google Ads account",
               "About 1 hour; products under review up to ~3 business days",
               "Build and host the Google-format feed, verify every field, walk through delivery and returns settings",
               "Own the Google account, give the legal address, choose countries, confirm delivery times and returns terms"])
        + row(["<b>Bing Shopping, Copilot</b><br>Microsoft Merchant Center",
               "Microsoft Advertising account (location locked at creation, phone number, legal business name and address), store imported from Google or a feed file",
               "Free; no card (payment can be set up later)",
               "About 20 minutes after Google; store approval up to 3 business days",
               "Prepare the same feed, submit product URLs through the Bing API with your key, IndexNow on WordPress",
               "Create the advertising account, verify the phone, allow the Google sign-in pop-up, click Create store"])
        + row(["<b>Perplexity shopping answers</b><br>Perplexity Merchant Program",
               "Five-step form in the perplexity.ai footer; must sell and ship to the US",
               "Free; no commission",
               "About 5 minutes; review time not published, reported in weeks",
               "Draft every answer, have the feed hosted, keep PerplexityBot unblocked",
               "Submit the form as the legal entity"])
        + row(["<b>Meta AI shopping</b> (optional)<br>Meta Commerce Manager",
               "Facebook login and a Business Portfolio; catalog via a scheduled data feed (Google format accepted)",
               "Free",
               "About 30 minutes; catalog review by Meta",
               "Provide the scheduled feed URL and the field mapping",
               "Own the Facebook business login and portfolio"])
        + row(["<b>ChatGPT Shopping</b><br>Shopify Catalog via Shopify&rsquo;s free Agentic plan",
               "A Shopify account on the free Agentic plan; catalog imported from a Shopify-format CSV; checkout stays on your own site; Shopify looks for a track record of genuine sales",
               "Free plan",
               "About 30 minutes to import; inclusion timing is Shopify&rsquo;s and OpenAI&rsquo;s",
               "Write the Shopify import CSV from your store, prepare the catalog",
               "Create the Shopify account, accept the plan terms, import"])
        + row(["<b>ChatGPT Shopping</b><br>Merchant application at chatgpt.com/merchants",
               "Spec-compliant OpenAI feed, OAI-SearchBot allowed, application reviewed from a waitlist; after approval, feed pushed by SFTP or API (OpenAI does not fetch URLs)",
               "Free to apply",
               "About 15 minutes to file; small merchants report long silences",
               "Build and validate the OpenAI-spec feed, draft the application, push by SFTP once approved",
               "Be the applicant of record; OpenAI decides"])
        + row(["<b>Search foundations</b><br>Google Search Console, Bing Webmaster Tools, sitemap",
               "DNS TXT record for Google; one-click import for Bing; a sitemap URL",
               "Free",
               "About 20 minutes",
               "Tell you where DNS lives, submit sitemaps and URL batches with your API key",
               "Add the TXT record, sign in to both tools"])
        + "</table></div>"
    )
    body = (
        '<div class="card lead"><p>Shopify and Etsy stores were integrated into ChatGPT Shopping automatically and have one-click channels to Google, Microsoft and Meta. A WooCommerce, BigCommerce, Magento or custom store has to enter every AI shopping surface itself. '
        'The surprising part, after doing it for a WooCommerce store on 3 September 2026: most of it fits in one day, most of it is free, and none of it needs a card. Here is the whole map &mdash; what each surface gates, what it costs, how long it takes, and the honest split between what a service can do and what only the merchant can do.</p></div>'

        '<div class="card"><h2>Every surface, side by side</h2>'
        '<p>Two files do most of the work: a Google Shopping-format product feed (accepted by Google, Microsoft, Meta and Perplexity) and, for ChatGPT, an OpenAI-spec feed plus a Shopify-format import CSV. The free <a href="/">connect form</a> builds all three from a store URL. Everything else is accounts.</p>'
        + table +
        '<p style="margin-top:12px">No row in this table is an inclusion promise. We prepare, validate and submit with you; each platform decides whether, when and where products appear.</p></div>'

        '<div class="card"><h2>The one-day order</h2>'
        '<p>The sequence matters: later steps import from earlier ones, and the two surfaces that approve within days should be done before the ones that answer in weeks.</p><ol>'
        '<li><b>Feed first (2 minutes).</b> Generate and host the Google-format feed so every later step has a URL to paste. Also confirm shipping, returns, privacy and terms pages are published on the store; three platforms read them.</li>'
        '<li><b>Google Search Console (15 minutes).</b> Domain property, TXT record at whoever hosts your DNS, submit the full sitemap URL. <a href="/google-merchant-center-woocommerce">Step 1 of the Google guide.</a></li>'
        '<li><b>Bing Webmaster Tools (5 minutes).</b> Import from Search Console, generate an API key, submit your product URLs (100 a day). <a href="/microsoft-merchant-center-copilot">Step 1 of the Microsoft guide.</a></li>'
        '<li><b>Google Merchant Center (45 minutes).</b> Sign up, legal address, US only if that is where you ship, feed by scheduled URL, delivery times entered manually, returns option matching your page. <a href="/google-merchant-center-woocommerce">Full guide.</a></li>'
        '<li><b>Microsoft Merchant Center (20 minutes).</b> Advertising account with no campaign and no card, store imported from Google, Import now, daily sync. <a href="/microsoft-merchant-center-copilot">Full guide.</a></li>'
        '<li><b>Perplexity (5 minutes).</b> Footer link, five steps, submit as the legal entity. <a href="/perplexity-merchant-program">Full guide.</a></li>'
        '<li><b>Meta Commerce Manager (optional, 30 minutes).</b> Only if you have or want a Facebook business login; scheduled feed in Google format.</li>'
        '<li><b>ChatGPT, both tracks (45 minutes).</b> Shopify free Agentic plan and catalog import from the Shopify CSV; then the merchant application at chatgpt.com/merchants with the OpenAI-spec feed ready and OAI-SearchBot allowed. <a href="/get-into-chatgpt-shopping">ChatGPT guide</a> and <a href="/openai-product-feed-spec">feed spec</a>.</li>'
        '</ol><p>Total: roughly ninety minutes of account work plus waiting. Then a week later, read the Products pages in Google and Microsoft and fix whatever they flag.</p></div>'

        '<div class="card"><h2>Gather these once; every platform asks</h2><ul>'
        '<li><b>The legal entity&rsquo;s name and registered address.</b> Google asks; Microsoft requires the legal name and can block on mismatch; Perplexity asks. Identical answers everywhere.</li>'
        '<li><b>A phone number</b> (Microsoft) and a contact email at your domain.</li>'
        '<li><b>Published policy pages</b>: shipping, returns, privacy, terms. Google checks the returns page against your setting; OpenAI requires privacy and terms URLs for checkout-eligible rows.</li>'
        '<li><b>Real delivery numbers</b> that match checkout, and <b>only the countries you ship to</b>.</li>'
        '<li><b>Crawler access</b>: robots.txt allowing Googlebot, Bingbot, OAI-SearchBot and PerplexityBot; product pages returning 200; a sitemap.</li>'
        '</ul></div>'

        '<div class="card"><h2>What to expect afterwards, honestly</h2>'
        '<p>Google and Microsoft respond in days and say exactly what is wrong when something is. Perplexity and OpenAI respond on their own schedule, if at all, and neither publishes a timeline. Shopify Catalog is documented as integrated with ChatGPT but wants sales history. '
        'So for a store that is not on Shopify: enter the four gate-free surfaces today, keep the ChatGPT tracks filed and feed-ready, keep Bing crawling your pages, and let the surfaces that respond start producing the orders that make the slower ones easier.</p></div>'

        + _cta("Start with the feed &mdash; free, one minute",
               "Enter your WooCommerce or Shopify store. We read the public catalog and build the Google-format feed, the OpenAI-spec feed and the Shopify import CSV, validated and hosted with a daily refresh. Then follow the guides above, or have us do the setup with you. You own every account; each platform decides.")
        + _related(("/google-merchant-center-woocommerce", "Google Merchant Center for WooCommerce"),
                   ("/microsoft-merchant-center-copilot", "Microsoft Merchant Center and Copilot"),
                   ("/perplexity-merchant-program", "Perplexity Merchant Program"),
                   ("/get-into-chatgpt-shopping", "Get into ChatGPT Shopping"),
                   ("/openai-product-feed-spec", "OpenAI product feed spec"),
                   ("/agentic-commerce-non-shopify", "Agentic commerce for non-Shopify stores"),
                   ("/how-it-works", "How CanAIShopYou works"))
    )
    return _render(
        "/ai-shopping-surfaces-checklist",
        "One-day checklist: every AI shopping surface a non-Shopify store can enter today",
        "Google AI Mode, Bing and Copilot, Perplexity, Meta AI and both ChatGPT routes in one table: what each gates, what it costs, how long it takes, and what only the merchant can do.",
        "One-day checklist: every <em>AI shopping surface</em> a non-Shopify store can enter today",
        "Google, Microsoft, Perplexity, Meta and both ChatGPT routes on one table &mdash; gate, cost, time, and the honest split between what we can do and what only you can do. Built from a real WooCommerce setup on 3 September 2026.",
        body, faqs)
