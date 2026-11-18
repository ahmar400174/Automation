"""Extract street addresses printed on business pages and linked map queries."""

import re
from urllib.parse import parse_qs, urlsplit, unquote


STREET = re.compile(
    r"\b\d{1,6}(?:-\d{1,6})?\s+(?:(?:[A-Za-z0-9.'’#-]+)\s+){0,7}"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Court|Ct|"
    r"Way|Highway|Hwy|Parkway|Pkwy|Place|Pl|Terrace|Ter|Circle|Cir|Trail|Trl|Broadway)\b\.?",
    re.I)
POSTAL = re.compile(r"\b(?:[A-Z]{2}\s+\d{5}(?:-\d{4})?|[A-Z]\d[A-Z]\s?\d[A-Z]\d)\b", re.I)
SUITE = re.compile(r"^\s*,?\s*(?:(?:Suite|Ste|Unit|Floor|Fl|#)\s*[A-Za-z0-9-]+)\b", re.I)


def addresses(text, links=(), city=""):
    sources = [text]
    for link in links:
        parsed = urlsplit(link["url"])
        host = (parsed.hostname or "").lower()
        if not (host == "maps.apple.com" or host == "maps.google.com"
                or host.endswith(".google.com") or host == "google.com"):
            continue
        query = parse_qs(parsed.query)
        for key in ("q", "query", "destination", "daddr", "address"):
            sources.extend(query.get(key, []))
        if "/maps/place/" in parsed.path:
            sources.append(unquote(parsed.path.split("/maps/place/", 1)[1].split("/", 1)[0]).replace("+", " "))
    found = {}
    for source in sources:
        source = re.sub(r"[\t\r\n\u00a0]+", " ", source)
        source = re.sub(r" +", " ", source)
        for match in STREET.finditer(source):
            street = match.group().strip().rstrip(".")
            tail = source[match.end():match.end() + 100]
            postal = POSTAL.search(tail)
            # Use the locality only when it ends in a recognizable postal address.
            if postal and not re.search(r"[|!?]|\b(?:phone|email|hours|copyright)\b", tail[:postal.end()], re.I):
                locality = tail[:postal.end()]
                if city and city.casefold() not in locality.casefold():
                    continue
                value = street + locality
            else:
                suite = SUITE.match(tail)
                value = street + (suite.group() if suite else "")
            key = re.sub(r"[^a-z0-9]", "", value.casefold())
            if len(value) > len(found.get(key, "")):
                found[key] = value.strip(" ,")
    return list(found.values())
