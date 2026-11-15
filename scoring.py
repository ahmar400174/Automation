"""All Scoring of a brand (one Facebook page) to rank the likely fashion dropshipping competitors.

Lightweight crash guard: everything public is defensively coded so a missing dict key, an unexpected
payload shape or a downstream failure never turns into a score that looks like a verified count."""
import os
import re
import logging

logger = logging.getLogger(__name__)

# ---- what the brand SELLS (clothing and shoes only; men and women) ----
FASHION = re.compile(r"(?i)\b(dress(?:es)?|gown|blouses?|shirts?|tees?|t-shirts?|tops?|tanks?|sweaters?|cardigans?|"
                     r"jackets?|coats?|blazers?|vests?|pants|trousers|jeans|denim|leggings|skirts?|shorts|jumpsuits?|"
                     r"rompers?|hoodies?|sweatshirts?|pullovers?|tunics?|kaftans?|capris|joggers|jumpers?|knitwear|"
                     r"boots?|booties|sneakers?|shoes?|sandals?|loafers?|clogs?|flats|heels|slippers?|wedges|pumps|"
                     r"outfits?|wardrobe|apparel|clothing|clothes|menswear|womenswear|silhouette|neckline|sleeves?|"
                     r"hem|waistband|cargo|polo|parka|poncho|kimono|cape|trench|"
                     r"vestidos?|blusas?|camisas?|camisetas?|pantal[oó]n(?:es)?|faldas?|chaquetas?|abrigos?|zapatos?|botas|sandalias|"
                     r"zapatillas|ropa|moda|jersey|vaqueros|"
                     r"robes?|chemises?|pantalons?|jupes?|vestes?|manteaux?|bottes|chaussures|baskets|sandales|v[eê]tements|mode|"
                     r"abiti?|vestit[oi]|camicia|camicie|maglia|maglie|pantaloni|gonn[ae]|giacc[ae]|cappott[oi]|stivali|scarpe|sandali|"
                     r"abbigliamento|moda|"
                     r"jurk(?:en)?|blouses?|overhemd(?:en)?|broeken?|rokken?|rok|jas|jassen|vest|trui|truien|schoenen|laarzen|"
                     r"sandalen|kleding|mode|damesmode|herenmode)\b")

# ---- what we do NOT want: accessories, jewelry, beauty, health, home, gadgets, lingerie ----
NONFASHION = re.compile(r"(?i)\b(rings?|necklaces?|bracelets?|bangles?|earrings?|pendants?|jewel\w*|moissanite|diamonds?|"
                        r"pearls?|gold-tone|charms?|watch(?:es)?|timepieces?|glasses|sunglasses|eyewear|"
                        r"bags?|handbags?|purses?|wallets?|belts?|scarf|scarves|hats?|caps?|"
                        r"lipsticks?|lip|lips|foundation|mascara|makeup|skincare|serum|cream|toothpaste|teeth|shaving|"
                        r"supplements?|vitamins?|blood sugar|pain|arthritis|joints?|wellness|health|healthy|podiatrist|"
                        r"fungus|nail|weight loss|calorie|"
                        r"bras?|bralette|lingerie|underwear|panties|shapewear|bikini|swimsuit|"
                        r"candles?|decor|kitchen|garden|furniture|pillow|mattress|bed|home|ceramics?|"
                        r"pets?|dogs?|cats?|toys?|baby|gadgets?|lock|tools?|golf|ants|spooky|"
                        r"joyas?|bolsos?|collar(?:es)?|anillos?|gafas|perros?|gatos?|hogar|"
                        r"bijoux|sacs?|bagues?|colliers?|lunettes|chiens?|chats?|"
                        r"borse?|gioielli|anelli|anello|occhiali|cani|gatti|casa|"
                        r"sieraden|ketting|armband|oorbellen|tas|tassen|bril|hond|honden|kat|katten|speelgoed|huis)\b")

# ---- storytelling / "closing sale" signals in the ad text (used for SORTING only) ----
STORY = re.compile(r"(?i)(anniversary|moving|retire\w*|closing|closed|clos(?:e|ing) (?:our|my|the)|revival|"
                   r"clearance|going out of business|last chance|final (?:sale|collection|days)|black friday|"
                   r"after \d+ years|for over \d+ years|\d+ years|family[- ]owned|small business|"
                   r"our story|my story|my husband|my mom|my daughter|grandma|grandmother|founder|"
                   r"thank you for (?:\d+|all)|goodbye|farewell|everything must go|must go|"
                   r"there'?s a (?:feeling|moment)|almost every woman|every woman (?:has|knows)|"
                   r"aniversario|liquidaci[oó]n|cierre|jubilaci[oó]n|cerramos|gracias por|"
                   r"anniversaire|liquidation|fermeture|retraite|merci pour|"
                   r"anniversario|chiusura|liquidazione|pensione|grazie per|"
                   r"jubileum|opheffing|uitverkoop|sluiting|pensioen|bedankt voor|dank(?:u)? wel)")

# ---- US states / big cities often added at the end of dropshipper page names ----
PLACES = re.compile(r"(?i)\b(alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|"
                    r"hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|"
                    r"michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|ohio|oklahoma|oregon|"
                    r"pennsylvania|tennessee|texas|utah|vermont|virginia|washington|wisconsin|wyoming|"
                    r"new york|new jersey|new hampshire|new mexico|north carolina|south carolina|north dakota|south dakota|"
                    r"west virginia|rhode island|ny|nyc|la|sf|dc|nj|fl|tx|ca|"
                    r"savannah|charleston|aspen|nashville|austin|dallas|houston|miami|boston|chicago|seattle|denver|"
                    r"atlanta|phoenix|brooklyn|manhattan|hamptons|malibu|napa|sedona|santa fe|boca raton|palm beach|"
                    r"scottsdale|jacksonville|orlando|tampa|portland|san diego|san francisco|los angeles|las vegas|"
                    r"london|paris|milan|rome|madrid|barcelona|dublin|manchester|lyon|marseille|nice|florence|venice|"
                    r"valencia|sevilla|malaga|ibiza|marbella|bilbao|mallorca|bordeaux|cannes|saint tropez|lille|nantes|"
                    r"milano|roma|firenze|napoli|venezia|torino|capri|positano|como|chelsea|kensington|mayfair|"
                    r"amsterdam|rotterdam|utrecht|den haag|eindhoven|haarlem|maastricht|antwerpen|brussel|gent|brugge)\b")

# first names / short given names often used in pairs ("Anne & Laura"); generic test: two capitalised words around & / and
PAIR = re.compile(r"^[A-Z][\w'’]+(?: [A-Z][\w'’]+)? (?:&|and|y|et|e|en) [A-Z][\w'’]+")
POSSESSIVE = re.compile(r"^[A-Z][a-z]+['’]s\b")
HOUSE = re.compile(r"(?i)^(?:the |maison |house of |atelier |boutique )")
CLOSET = re.compile(r"(?i)\b(closet|boutique|atelier|maison|collective|edit|studio|house|lane|row|market|co)\b")


def _texts(b):
    if not isinstance(b, dict):
        return ""
    copies = b.get("copies") or ([b["copy"]] if b.get("copy") else [])
    try:
        return " ".join(str(c) for c in copies if c)
    except Exception:
        return ""


LOCAL_TLD = {"ES": (".es",), "FR": (".fr",), "IT": (".it",), "GB": (".uk", ".co.uk"), "NL": (".nl",)}

# common words of each local language: 3+ different ones in the ad text = ad written in the local language
LOCAL_LANG = {
    "ES": re.compile(r"(?i)\b(los|las|del|para|con|una|por|nuestra|nuestro|tu|tus|envío|gratis|oferta|compra|ahora|todo|hasta|más)\b"),
    "FR": re.compile(r"(?i)\b(les|des|pour|avec|une|nous|votre|vos|livraison|gratuite|offre|maintenant|tout|jusqu|plus|notre)\b"),
    "IT": re.compile(r"(?i)\b(gli|per|con|una|della|delle|nostra|nostro|tuo|spedizione|gratuita|offerta|ora|tutto|fino|più|il|la)\b"),
    "NL": re.compile(r"(?i)\b(het|een|voor|met|onze|jouw|jouw|gratis|verzending|aanbieding|nu|alles|tot|meer|bij|niet|ons)\b"),
}


def score_brand(b, country=None):
    """Returns (score, category, reasons). category: 'fashion' | 'other' | 'unknown'."""
    if not isinstance(b, dict):
        return 0, "unknown", ["bad brand record"]
    name = str(b.get("name") or "")
    domain = str(b.get("domain") or "").replace("-", " ").replace(".", " ")
    text = _texts(b)

    fash = set(m.lower() for m in FASHION.findall(text + " " + name + " " + domain))
    non = set(m.lower() for m in NONFASHION.findall(text + " " + name + " " + domain))

    reasons, score = [], 0
    if fash:
        score += min(len(fash), 3)
        reasons.append("fashion words: " + ", ".join(sorted(fash)[:4]))
    if non:
        reasons.append("non-fashion words: " + ", ".join(sorted(non)[:4]))

    if not fash and non:
        cat = "other"
    elif non and len(non) > len(fash):
        cat = "other"
    elif fash:
        cat = "fashion"
    else:
        cat = "unknown"

    if PAIR.search(name):
        score += 3; reasons.append("name: two names (X & Y)")
    elif POSSESSIVE.search(name):
        score += 2; reasons.append("name: owner style (Linda's ...)")
    if PLACES.search(name):
        score += 2; reasons.append("name: city/state")
    if HOUSE.search(name) or CLOSET.search(name):
        score += 1; reasons.append("name: boutique style")

    dom = str(b.get("domain") or "").lower()
    tlds = LOCAL_TLD.get((country or "").upper(), ())
    if tlds and any(dom.endswith(t) for t in tlds):
        score += 3; reasons.append("local domain (" + dom.rsplit(".", 1)[-1] + ")")

    lang = LOCAL_LANG.get((country or "").upper())
    if lang:
        try:
            hits = set(m.lower() for m in lang.findall(text))
            if len(hits) >= 3:
                score += 2; reasons.append("ad text in local language")
        except Exception:
            logger.warning("lang scoring failed", exc_info=True)

    st = set(m.lower() if isinstance(m, str) else m[0].lower() for m in STORY.findall(text))
    if st:
        score += min(2 * len(st), 6)
        reasons.append("story: " + ", ".join(sorted(st)[:3]))
    # Competitor signal = ads that are STILL RUNNING, not every ad ever indexed in the period. The
    # page-level count the API publishes (active_ads, what the dashboard shows) decides the point; only a
    # payload without that count falls back to the scan's own strictly-live rows.
    try:
        page_active = b.get("active_ads")
        ads = b.get("ads", 0) or 0
        live = b.get("live_ads")
        checked = b.get("checked_ads") or 0
        if isinstance(page_active, int):
            signal, confirmed = page_active, True
        else:
            confirmed = ads > 0 and checked >= ads
            signal = live if (confirmed and live is not None) else ads
        if signal >= 5:
            score += 1; reasons.append("5+ live ads" if confirmed else "5+ ads")
        elif confirmed and signal == 0:
            score -= 2; reasons.append("no ad still running")
    except Exception:
        logger.warning("score_brand active-signal failed", exc_info=True)
    return score, cat, reasons


# ---------------------------------------------------------------------------------------------------
# Report hygiene: what may never reach a report (dashboard or workbook).
# Applied by wh_scan.py (per country) and run_weekly.py (merged report), always AFTER the API payloads
# were parsed and BEFORE anything is written, so a fake or empty entry cannot be exported.
# ---------------------------------------------------------------------------------------------------
# A competitor STORE is never a social network, a link-in-bio hub, a URL shortener, a search engine, an
# email provider or a marketplace: those are the links that must be dropped (facebook.com, instagram.com,
# linktr.ee, amazon.com, ...). Matched on the host, so m.facebook.com is caught too.
GENERIC_HOSTS = {
    # social networks & messengers
    "facebook.com", "fb.com", "fb.me", "fb.watch", "fbcdn.net", "fbsbx.com",
    "instagram.com", "instagr.am", "threads.net", "tiktok.com", "snapchat.com",
    "youtube.com", "youtu.be", "twitter.com", "x.com", "tumblr.com", "vk.com",
    "reddit.com", "linkedin.com", "t.me", "telegram.me", "telegram.org",
    "whatsapp.com", "chat.whatsapp.com", "wa.me", "discord.com", "discord.gg", "messenger.com",
    # link-in-bio hubs
    "linktr.ee", "linkr.bio", "linkin.bio", "beacons.ai", "beacons.page", "taplink.cc", "bio.link",
    "komi.io", "linkpop.com", "shor.by", "carrd.co", "later.com",
    # URL shorteners
    "bit.ly", "tinyurl.com", "t.co", "rebrand.ly", "cutt.ly", "is.gd", "ow.ly", "goo.gl",
    # search engines / email / big platforms
    "bing.com", "duckduckgo.com", "apple.com", "spotify.com", "wikipedia.org", "archive.org",
    "imgur.com", "gravatar.com", "gmail.com", "googlemail.com", "outlook.com", "mail.com",
    "google.com", "yandex.com", "yahoo.com", "hotmail.com",
    # marketplaces
    "etsy.com", "alibaba.com", "temu.com", "shein.com", "wish.com", "cdiscount.com",
    "amazon.com", "ebay.com", "aliexpress.com", "allegro.pl", "zalando.com",
    # site builders / file hosts / payment pages / other non-stores
    "canva.com", "drive.google.com", "dropbox.com", "stripe.com", "paypal.com",
    "wordpress.com", "blogspot.com", "wix.com", "wixsite.com", "squarespace.com",
    "weebly.com", "godaddysites.com", "mystrikingly.com", "gumroad.com", "patreon.com",
    "trustpilot.com", "cloudfront.net",
}

# Platforms that own one domain per country (amazon.co.uk, pinterest.fr, yahoo.de, blogspot.com...):
# matched on their first label, so every country variant is caught without listing each TLD.
GENERIC_BRANDS = {
    "amazon", "ebay", "aliexpress", "paypal", "allegro", "zalando", "pinterest",
    "google", "yahoo", "hotmail", "yandex", "blogspot",
}

# A VERIFIED competitor has to run at least this many ads right now. An authoritative inactive zero
# (Meta showed the page runs no ad) is kept separately and displayed as 0 — see is_active_row.
MIN_ACTIVE_ADS = 1
IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
TLD = re.compile(r"^[a-z]{2,24}$")


def clean_domain(domain):
    """The comparable host of a site link: no scheme, no www, no path/query/port, no trailing dot."""
    try:
        d = (domain or "").strip().lower()
        d = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", d)
        d = d.split("/")[0].split("?")[0].split("#")[0].split(":")[0].strip().strip(".")
        d = re.sub(r"^www\d?\.", "", d)
        return d
    except Exception:
        return ""


def is_generic_host(domain):
    """True for a platform link (social network, link hub, shortener, marketplace…) that is never a store.

    GENERIC_HOSTS is a plain set: a host matches when it equals an entry or is a subdomain of one
    (m.facebook.com -> facebook.com); GENERIC_BRANDS catches the country TLDs of the big platforms.
    """
    d = clean_domain(domain)
    if not d or "." not in d:
        return False
    labels = d.split(".")
    if any(".".join(labels[i:]) in GENERIC_HOSTS for i in range(len(labels) - 1)):
        return True
    return labels[0] in GENERIC_BRANDS


def is_real_site(domain):
    """True only for a plausible competitor store domain: a name, a real TLD, not a generic platform."""
    d = clean_domain(domain)
    if len(d) < 4 or "." not in d or " " in d or IPV4.match(d):
        return False
    if not TLD.match(d.rsplit(".", 1)[-1]):
        return False
    return not is_generic_host(d)


VERIFIED_STATUSES = ("verified",)
# Meta's AUTHORITATIVE zero (it showed the page runs no ad, double-checked) is published with 0 active ads.
ZERO_STATUSES = ("inactive",)
VALID_STATUSES = ("verified", "inactive", "unverified", "stale", "verification_failed")
# Statuses whose Active Ads number is a real Meta answer (a count, or an authoritative zero).
TRUSTED_STATUSES = VERIFIED_STATUSES + ZERO_STATUSES


def is_report_candidate(domain, status):
    """Whether a scraped store belongs in the competitor list (dashboard + workbook).

    EVERY real competitor store is listed, verified or not. The status column says how far the number can
    be trusted: verified / inactive rows carry a real Meta count, while unverified / stale /
    verification_failed rows are listed with NO number (shown as a dash, never as a made-up count and never
    as a fake 0). Only a generic platform link (facebook.com, linktr.ee, ...) is never a competitor.
    """
    allowed = TRUSTED_STATUSES if os.environ.get("WH_STRICT", "0") == "1" else VALID_STATUSES
    # WH_STRICT=1 (workflow input "strict"): only competitors with a real Meta answer are listed.
    return is_real_site(domain) and str(status or "").strip().lower() in allowed


def is_active_row(domain, active, status="verified"):
    """Whether a row has a trustworthy numeric Meta active-ad count.

    A row is published only when BOTH hold: it has a real site link, and its active count is
    trustworthy. The client's rule is that an inactive advertiser stays visible with 0:

      * verified      - `active` is Meta's live count for this page/region: it must reach MIN_ACTIVE_ADS
                        to be published as a real positive active count (a verified 0 is normalised to
                        "inactive" and published as 0)
      * inactive      - Meta authoritatively showed 0 running ads: the row is KEPT and the report shows 0
                        in Active Ads (inactive advertisers stay visible)
      * unverified / stale / verification_failed - not a trustworthy numeric count. The row is still listed
                        as a competitor (see is_report_candidate) but without any number.

    A missing/invalid count for a verified row is refused. The default status keeps the gate strict for
    callers that only have a number.
    """
    if not is_real_site(domain):
        return False                      # no real store link -> never a competitor row
    state = str(status or "").strip().lower()
    if state in ZERO_STATUSES:
        return True                       # Meta's authoritative zero: kept, displayed as 0
    if state in VERIFIED_STATUSES:
        try:
            return int(float(str(active).strip())) >= MIN_ACTIVE_ADS
        except (TypeError, ValueError):
            return False                  # a verified row without a real live count is not verified
    # unverified / stale / verification_failed / unknown: no trustworthy count -> not in the report
    return False


def is_number(value):
    """True when *value* is already a real number, not the string "0" re-parsed from a missing or
    unverified stash. The report writes 0/1/2/... as Python ints after reporting; callers that still pass
    a stale/stashed/unverified row will still hand us None (or a string we can't turn into an int), so in
    those cases we refuse to let the number-based helpers ever treat it as a verified count.
    """
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    except Exception:
        return False
