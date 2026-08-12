#!/usr/bin/env python3
"""
Apartment monitor -> Telegram. Watches multiple portals and messages you once
per brand-new matching listing.

Sources:
  * bina.az     — current GraphQL API (server-side filters, rich data + photo)
  * yeniemlak.az — server-rendered HTML (parsed directly; no signature needed)

Memory lives in seen.json via the GitHub Contents API (atomic, no git races).
Each source is seeded silently the first time it appears, so adding a new portal
never floods you with its existing listings.
"""
import base64
import datetime as dt
import html
import json
import os
import re
import sys
from urllib.parse import parse_qs, urlparse

import requests

# --------------------------------------------------------------------------- #
# YOUR SEARCHES  (paste the normal search URL from each site's address bar)
# --------------------------------------------------------------------------- #
BINA_SEARCH_URL = os.environ.get("BINA_SEARCH_URL", (
    "https://bina.az/baki/alqi-satqi/menziller?room_ids%5B%5D=2&room_ids%5B%5D=3&room_ids%5B%5D=4&price_to=190000&area_from=55&has_bill_of_sale=true&has_mortgage=true&floor_first=false&floor_last=false&location_ids%5B%5D=8&location_ids%5B%5D=51&location_ids%5B%5D=2&location_ids%5B%5D=33&location_ids%5B%5D=54&location_ids%5B%5D=4&location_ids%5B%5D=52&location_ids%5B%5D=53&location_ids%5B%5D=1&location_ids%5B%5D=259&location_ids%5B%5D=314&location_ids%5B%5D=100&location_ids%5B%5D=99&location_ids%5B%5D=233&location_ids%5B%5D=68&location_ids%5B%5D=74&location_ids%5B%5D=69&location_ids%5B%5D=103&location_ids%5B%5D=246&location_ids%5B%5D=186&location_ids%5B%5D=376&location_ids%5B%5D=25&location_ids%5B%5D=138&location_ids%5B%5D=152&location_ids%5B%5D=26&location_ids%5B%5D=175&location_ids%5B%5D=135&location_ids%5B%5D=136&location_ids%5B%5D=16&location_ids%5B%5D=36"
))
YENIEMLAK_SEARCH_URL = os.environ.get("YENIEMLAK_SEARCH_URL", (
    "https://yeniemlak.az/elan/axtar?elan_nov=1&emlak=1&menzil_nov=&qiymet=&qiymet2=195000&mertebe=2&mertebe2=&otaq=2&otaq2=&sahe_m=50&sahe_m2=&sahe_s=&sahe_s2=&seher%5B%5D=7&rayon%5B%5D=2&rayon%5B%5D=3&rayon%5B%5D=6&rayon%5B%5D=9&menteqe%5B%5D=20&menteqe%5B%5D=23&menteqe%5B%5D=45&menteqe%5B%5D=72&menteqe%5B%5D=73&menteqe%5B%5D=74&metro%5B%5D=1&metro%5B%5D=2&metro%5B%5D=3&metro%5B%5D=4&metro%5B%5D=5&metro%5B%5D=8&metro%5B%5D=9&metro%5B%5D=10&metro%5B%5D=18&metro%5B%5D=19"
))

SOURCES = [
    {"name": "bina.az", "type": "bina", "url": BINA_SEARCH_URL, "prefix": ""},
    {"name": "yeniemlak.az", "type": "yeniemlak", "url": YENIEMLAK_SEARCH_URL, "prefix": "ye:"},
]

# bina.az config
CITY_ID = os.environ.get("BINA_CITY_ID", "1")
CATEGORY_ID = os.environ.get("BINA_CATEGORY_ID", "1")
PERSISTED_HASH = os.environ.get("BINA_PERSISTED_HASH",
    "b781511a943a4d710eefdf811a24dd4ae353e55d836952603ce0b37fde97d073")
GRAPHQL_URL = "https://bina.az/graphql"
OPERATION = "SearchItems"
SORT = "BUMPED_AT_DESC"
PAGE_SIZE = 16
SCAN_PAGES = int(os.environ.get("SCAN_PAGES", "6"))

# general config
STATE_FILE = os.environ.get("STATE_FILE", "seen.json")
MAX_SEEN = 8000
SEND_PHOTOS = os.environ.get("SEND_PHOTOS", "true").lower() == "true"

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

GH_TOKEN = os.environ.get("GH_TOKEN", "").strip()
GH_REPO = os.environ.get("GH_REPO", "").strip()
GH_BRANCH = os.environ.get("GH_BRANCH", "main").strip()
USE_API = bool(GH_TOKEN and GH_REPO)
IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
API_HEADERS = {"User-Agent": UA, "Accept": "*/*",
               "Accept-Language": "az,en-US;q=0.9,en;q=0.8,ru;q=0.7",
               "Content-Type": "application/json",
               "Referer": "https://bina.az/baki/alqi-satqi/menziller",
               "Origin": "https://bina.az", "x-platform": "desktop"}
HTML_HEADERS = {"User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "az,en-US;q=0.9,en;q=0.8,ru;q=0.7"}


def log(*a):
    print(*a, flush=True)


class PersistedQueryError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def tg_send_message(text):
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                                "disable_web_page_preview": False}, timeout=30)
        if r.status_code == 200:
            return True
        log("Telegram sendMessage failed:", r.status_code, r.text[:200])
        return False
    except requests.RequestException as e:
        log("Telegram error:", e)
        return False


def tg_send_photo(photo_url, caption):
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                          json={"chat_id": CHAT_ID, "photo": photo_url, "caption": caption,
                                "parse_mode": "HTML"}, timeout=30)
        if r.status_code == 200:
            return True
        return tg_send_message(caption)
    except requests.RequestException:
        return tg_send_message(caption)


# --------------------------------------------------------------------------- #
# Source 1: bina.az GraphQL
# --------------------------------------------------------------------------- #
def bina_filter_vars(url):
    q = parse_qs(urlparse(url).query)

    def one(k):
        v = q.get(k)
        return v[0] if v else None

    def b(v):
        return str(v).lower() in ("1", "true", "yes", "on") if v is not None else None

    def num(v):
        try:
            f = float(v)
            return int(f) if f.is_integer() else f
        except (TypeError, ValueError):
            return None

    f = {"cityId": CITY_ID, "categoryId": CATEGORY_ID, "leased": False}
    rooms = [str(v) for v in q.get("room_ids[]", []) if str(v).strip()]
    if rooms:
        f["roomIds"] = rooms
    locs = [str(v) for v in q.get("location_ids[]", []) if str(v).strip()]
    if locs:
        f["locationIds"] = locs
    if num(one("price_to")) is not None:
        f["priceTo"] = num(one("price_to"))
    if num(one("price_from")) is not None:
        f["priceFrom"] = num(one("price_from"))
    if num(one("area_from")) is not None:
        f["areaFrom"] = num(one("area_from"))
    if num(one("area_to")) is not None:
        f["areaTo"] = num(one("area_to"))
    if b(one("has_bill_of_sale")) is not None:
        f["hasBillOfSale"] = b(one("has_bill_of_sale"))
    if b(one("has_mortgage")) is not None:
        f["hasMortgage"] = b(one("has_mortgage"))
    f["floorFirst"] = b(one("floor_first")) is True
    f["floorLast"] = b(one("floor_last")) is True
    return f


def _bina_params(filter_vars, cursor):
    variables = {"first": PAGE_SIZE, "filter": filter_vars, "sort": SORT}
    if cursor:
        variables["cursor"] = cursor
    return {"operationName": OPERATION,
            "variables": json.dumps(variables, separators=(",", ":"), ensure_ascii=False),
            "extensions": json.dumps(
                {"persistedQuery": {"version": 1, "sha256Hash": PERSISTED_HASH}},
                separators=(",", ":"))}


def _bina_node(node):
    def sub(k, fld):
        o = node.get(k)
        return o.get(fld) if isinstance(o, dict) else None

    preview = node.get("preview") or {}
    photo = preview.get("f460x345") or preview.get("thumbnail")
    area = sub("area", "value")
    try:
        area = float(area) if area is not None else None
    except (TypeError, ValueError):
        area = None
    price = sub("price", "total")
    try:
        price = int(float(price)) if price is not None else None
    except (TypeError, ValueError):
        price = None
    loc_id = sub("location", "id")
    try:
        loc_id = int(loc_id) if loc_id is not None else None
    except (TypeError, ValueError):
        loc_id = None
    path = node.get("path")
    return {"id": str(node["id"]), "rooms": node.get("rooms"), "area": area,
            "area_units": sub("area", "units") or "m²", "floor": node.get("floor"),
            "floors": node.get("floors"), "price": price,
            "currency": sub("price", "currency") or "AZN", "location_id": loc_id,
            "location": sub("location", "fullName") or sub("location", "name") or sub("city", "name"),
            "has_bill_of_sale": node.get("hasBillOfSale"), "has_mortgage": node.get("hasMortgage"),
            "has_repair": node.get("hasRepair"), "updated_at": node.get("updatedAt"),
            "url": f"https://bina.az{path}" if path else f"https://bina.az/items/{node['id']}",
            "photo": photo}


def bina_check(url):
    q = parse_qs(urlparse(url).query)

    def one(k):
        v = q.get(k)
        return v[0] if v else None

    def i(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    return {"rooms": {i(v) for v in q.get("room_ids[]", []) if i(v) is not None},
            "locs": {i(v) for v in q.get("location_ids[]", []) if i(v) is not None},
            "price_to": i(one("price_to")),
            "area_from": float(one("area_from")) if one("area_from") else None}


def bina_passes(l, c):
    if c["rooms"] and l.get("rooms") not in c["rooms"]:
        return False
    if c["price_to"] is not None and (l.get("price") is None or l["price"] > c["price_to"]):
        return False
    if c["area_from"] is not None and (l.get("area") is None or l["area"] < c["area_from"]):
        return False
    if c["locs"]:
        if l.get("location_id") is None or l["location_id"] not in c["locs"]:
            return False
    return True


def fetch_bina(url):
    fv = bina_filter_vars(url)
    check = bina_check(url)
    out, cursor = [], None
    for _ in range(SCAN_PAGES):
        r = requests.get(GRAPHQL_URL, params=_bina_params(fv, cursor), headers=API_HEADERS, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        try:
            payload = r.json()
        except ValueError:
            raise RuntimeError("response was not JSON")
        if payload.get("errors"):
            msg = "; ".join(str(e.get("message", e)) for e in payload["errors"])
            if "PersistedQueryNotFound" in msg or "PERSISTED_QUERY_NOT_FOUND" in msg:
                raise PersistedQueryError(msg)
            raise RuntimeError(f"GraphQL error: {msg}")
        conn = (payload.get("data") or {}).get("itemsConnection")
        if not conn:
            raise RuntimeError("no itemsConnection")
        for edge in conn.get("edges", []):
            node = edge.get("node")
            if node and node.get("id") is not None:
                try:
                    l = _bina_node(node)
                    if bina_passes(l, check):
                        out.append(l)
                except Exception as e:
                    log("skip bina node:", e)
        info = conn.get("pageInfo") or {}
        if not info.get("hasNextPage") or not info.get("endCursor"):
            break
        cursor = info["endCursor"]
    return out


# --------------------------------------------------------------------------- #
# Source 2: yeniemlak.az HTML
# --------------------------------------------------------------------------- #
def yeniemlak_check(url):
    q = parse_qs(urlparse(url).query)

    def n(k):
        v = q.get(k, [None])[0]
        return int(v) if v and str(v).isdigit() else None
    return {"price_max": n("qiymet2"), "price_min": n("qiymet"),
            "rooms_min": n("otaq"), "rooms_max": n("otaq2"),
            "area_min": n("sahe_m"), "area_max": n("sahe_m2"),
            "floor_min": n("mertebe"), "floor_max": n("mertebe2")}


def yeniemlak_passes(l, c):
    p, rm, ar, fl = l.get("price"), l.get("rooms"), l.get("area"), l.get("floor")
    if c["price_max"] and p and p > c["price_max"]:
        return False
    if c["price_min"] and p and p < c["price_min"]:
        return False
    if c["rooms_min"] and rm and rm < c["rooms_min"]:
        return False
    if c["rooms_max"] and rm and rm > c["rooms_max"]:
        return False
    if c["area_min"] and ar and ar < c["area_min"]:
        return False
    if c["area_max"] and ar and ar > c["area_max"]:
        return False
    if c["floor_min"] and fl and fl < c["floor_min"]:
        return False
    if c["floor_max"] and fl and fl > c["floor_max"]:
        return False
    return True


def fetch_yeniemlak(url):
    r = requests.get(url, headers=HTML_HEADERS, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    raw = r.text
    id_url = {}
    for m in re.finditer(r'/elan/([A-Za-z0-9\-]*?-(\d{5,}))', raw):
        id_url.setdefault(m.group(2), "https://yeniemlak.az/elan/" + m.group(1))

    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)

    total_m = re.search(r"Nəticə:\s*(\d+)", text)
    total = int(total_m.group(1)) if total_m else None

    check = yeniemlak_check(url)
    heads = [m.start() for m in re.finditer(r"(?:Satılır|Kirayə|Girov)\s*\d[\d ]*?\s*Baxış", text)]
    heads.append(len(text))
    listings, seen_ids = [], set()
    for i in range(len(heads) - 1):
        b = text[heads[i]:heads[i + 1]]
        m_id = re.search(r"Elan:\s*(\d+)", b)
        if not m_id:
            continue
        iid = m_id.group(1)
        if iid in seen_ids:
            continue
        seen_ids.add(iid)

        def g(pat, grp=1, cast=None):
            m = re.search(pat, b)
            if not m:
                return None
            v = m.group(grp)
            return cast(v) if cast else v

        price_m = re.search(r"(?:Satılır|Kirayə|Girov)\s*(\d[\d ]*?)\s*Baxış", b)
        price = int(price_m.group(1).replace(" ", "")) if price_m else None
        floor_m = re.search(r"(\d+)\s*/\s*(\d+)\s*Mərtəbə", b)
        floors = int(floor_m.group(1)) if floor_m else None   # total floors
        floor = int(floor_m.group(2)) if floor_m else None    # actual floor
        loc = None
        lm = re.search(r"Ünvan:\s*(.+)", b)
        if lm:
            raw_loc = lm.group(1).strip()
            mm = re.search(r"(.*?metro\.\s*\S+(?:\s\S+)?)", raw_loc)
            loc = (mm.group(1) if mm else raw_loc[:70]).strip()

        l = {"id": iid,
             "url": id_url.get(iid, f"https://yeniemlak.az/elan/{iid}"),
             "rooms": g(r"(\d+)\s*otaq", cast=int),
             "area": g(r"(\d+)\s*m2", cast=int),
             "area_units": "m²", "floor": floor, "floors": floors,
             "price": price, "currency": "AZN",
             "location": loc, "location_id": None,
             "updated_at": g(r"Tarix:\s*([\d.]+)"),
             "has_bill_of_sale": None, "has_mortgage": None, "has_repair": None,
             "photo": None}
        if yeniemlak_passes(l, check):
            listings.append(l)

    if not listings and total and total > 0:
        raise RuntimeError(f"parsed 0 of {total} listings (page structure changed?)")
    return listings


def fetch_source(source):
    if source["type"] == "bina":
        return fetch_bina(source["url"])
    if source["type"] == "yeniemlak":
        return fetch_yeniemlak(source["url"])
    raise RuntimeError(f"unknown source type {source['type']}")


# --------------------------------------------------------------------------- #
# State (GitHub Contents API, atomic)
# --------------------------------------------------------------------------- #
def _gh_headers():
    return {"Authorization": f"Bearer {GH_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def _gh_url():
    return f"https://api.github.com/repos/{GH_REPO}/contents/{STATE_FILE}"


def load_state():
    if not USE_API:
        if not os.path.exists(STATE_FILE):
            return {"listings": {}}, None
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        data.setdefault("listings", {})
        return data, None
    r = requests.get(_gh_url(), headers=_gh_headers(), params={"ref": GH_BRANCH}, timeout=30)
    if r.status_code == 404:
        return {"listings": {}}, None
    r.raise_for_status()
    j = r.json()
    raw = base64.b64decode(j.get("content", "")).decode("utf-8") if j.get("content") else ""
    state = json.loads(raw) if raw.strip() else {"listings": {}}
    state.setdefault("listings", {})
    return state, j["sha"]


def _prune(state):
    L = state["listings"]
    if len(L) > MAX_SEEN:
        kept = sorted(L.items(), key=lambda kv: kv[1].get("first_seen", ""), reverse=True)[:MAX_SEEN]
        state["listings"] = dict(kept)


def save_state(state, sha):
    _prune(state)
    if not USE_API:
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=1)
            return True, "local"
        except Exception as e:
            return False, f"local write: {e}"
    reason = "unknown"
    for _ in range(5):
        body = {"message": "Update seen listings", "branch": GH_BRANCH,
                "content": base64.b64encode(
                    json.dumps(state, ensure_ascii=False).encode("utf-8")).decode("ascii")}
        if sha:
            body["sha"] = sha
        r = requests.put(_gh_url(), headers=_gh_headers(), json=body, timeout=30)
        if r.status_code in (200, 201):
            return True, "saved"
        if r.status_code in (409, 422):
            reason = f"HTTP {r.status_code} (retrying)"
            g = requests.get(_gh_url(), headers=_gh_headers(), params={"ref": GH_BRANCH}, timeout=30)
            if g.status_code == 200:
                j = g.json()
                sha = j["sha"]
                raw = base64.b64decode(j.get("content", "")).decode("utf-8") if j.get("content") else ""
                latest = json.loads(raw) if raw.strip() else {"listings": {}}
                latest.setdefault("listings", {})
                for k, v in state["listings"].items():
                    latest["listings"].setdefault(k, v)
                state = latest
                _prune(state)
                continue
            reason = f"re-read failed HTTP {g.status_code}"
            break
        reason = f"HTTP {r.status_code}: {r.text[:140]}"
        log("save_state failed:", reason)
        return False, reason
    return False, reason


# --------------------------------------------------------------------------- #
# Message
# --------------------------------------------------------------------------- #
def _fmt_pub(v):
    if not v:
        return None
    try:
        return dt.datetime.fromisoformat(str(v)).strftime("%d %b %Y, %H:%M")
    except Exception:
        return str(v)


def format_message(l, source_name):
    lines = [f"🏠 <b>NEW APARTMENT FOUND</b> · {html.escape(source_name)}", ""]
    if l.get("price") is not None:
        lines.append(f"💰 <b>Price:</b> {l['price']:,} {l.get('currency', 'AZN')}")
    lines.append(f"🛏 <b>Rooms:</b> {l.get('rooms') if l.get('rooms') is not None else '-'}")
    if l.get("area") is not None:
        area = int(l["area"]) if float(l["area"]).is_integer() else l["area"]
        lines.append(f"📐 <b>Area:</b> {area} {l.get('area_units', 'm²')}")
    if l.get("floor") and l.get("floors"):
        lines.append(f"🏢 <b>Floor:</b> {l['floor']}/{l['floors']}")
    elif l.get("floor"):
        lines.append(f"🏢 <b>Floor:</b> {l['floor']}")
    if l.get("location"):
        lines.append(f"📍 <b>Location:</b> {html.escape(str(l['location']))}")
    pub = _fmt_pub(l.get("updated_at"))
    if pub:
        lines.append(f"📅 <b>Published:</b> {html.escape(pub)}")
    tags = [t for t, on in (("kupçalı", l.get("has_bill_of_sale")),
                            ("ipoteka", l.get("has_mortgage")),
                            ("təmirli", l.get("has_repair"))) if on]
    if tags:
        lines.append("✅ " + ", ".join(tags))
    lines.append("")
    lines.append(f'🔗 <a href="{html.escape(l["url"])}">Open listing</a>')
    return "\n".join(lines)


def notify(l, source_name):
    text = format_message(l, source_name)
    if SEND_PHOTOS and l.get("photo"):
        return tg_send_photo(l["photo"], text)
    return tg_send_message(text)


# --------------------------------------------------------------------------- #
# Per-source processing
# --------------------------------------------------------------------------- #
def source_seeded(seen, prefix):
    """Has this source ever been recorded before?"""
    if prefix == "":                     # bina.az uses bare numeric keys
        return any(not k.startswith("ye:") for k in seen)
    return any(k.startswith(prefix) for k in seen)


def process_source(items, source, seen):
    prefix, name = source["prefix"], source["name"]
    seeded = source_seeded(seen, prefix)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    fresh = []
    recorded = 0
    for l in items:
        key = prefix + str(l["id"])
        if key in seen:
            continue
        if seeded:
            fresh.append((key, l))
        else:
            seen[key] = {"url": l["url"], "price": l.get("price"), "first_seen": now,
                         "notification_sent": True, "matched": True, "source": name}
            recorded += 1

    if not seeded:
        tg_send_message(f"✅ Now also monitoring <b>{html.escape(name)}</b> — recorded "
                        f"{recorded} current listings. You'll get only new ones from here.")
        return 0

    notified = 0
    for key, l in fresh:
        if notify(l, name):
            seen[key] = {"url": l["url"], "price": l.get("price"), "first_seen": now,
                         "notification_sent": True, "matched": True, "source": name}
            notified += 1
        else:
            log("send failed, retry next run:", key)
    return notified


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    if not BOT_TOKEN or not CHAT_ID:
        log("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set.")
        sys.exit(1)

    try:
        state, sha = load_state()
    except Exception as e:
        log("Could not load state; skipping to avoid duplicates:", e)
        if IN_ACTIONS:
            tg_send_message(f"⚠️ Could not READ memory this run ({html.escape(str(e))}). Skipping.")
        return
    seen = state["listings"]

    if IN_ACTIONS and not USE_API:
        tg_send_message("⚠️ Memory storage not configured (GH_TOKEN/GH_REPO missing); listings "
                        "will repeat. Check the workflow env block.")

    total_notified = 0
    for source in SOURCES:
        try:
            items = fetch_source(source)
        except PersistedQueryError:
            tg_send_message("⚠️ bina.az signature expired — capture a fresh one (README) and "
                            "update BINA_PERSISTED_HASH. bina.az paused; other sources continue.")
            continue
        except Exception as e:
            log(f"{source['name']} fetch failed:", e)
            continue
        total_notified += process_source(items, source, seen)

    ok, reason = save_state(state, sha)
    if not ok:
        tg_send_message("⚠️ Read listings fine, but could NOT save memory — listings will "
                        f"repeat until fixed.\nReason: {html.escape(reason)}")
    log(f"Done. notified={total_notified} saved={ok}({reason}) seen_total={len(seen)}")


if __name__ == "__main__":
    main()
