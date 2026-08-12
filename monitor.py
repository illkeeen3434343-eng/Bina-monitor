#!/usr/bin/env python3
"""
Bina.az -> Telegram apartment monitor (resilient single-run edition).

Reads the newest listings for your saved search and messages you about brand-new
ones. It tries Bina's fast GraphQL API first; if that is unavailable (e.g. the
request signature has expired after a site update), it automatically falls back
to reading Bina's normal search-results web page, which needs no signature and
already applies your filters server-side. It always reports what it did.
"""
import datetime as dt
import html
import json
import os
import re
import sys
from urllib.parse import parse_qs, urlparse

import requests

# ---- The search you want to monitor (paste any bina.az search URL here) ----
BINA_SEARCH_URL = os.environ.get("BINA_SEARCH_URL", (
    "https://bina.az/baki/alqi-satqi/menziller?room_ids%5B%5D=2&room_ids%5B%5D=3&room_ids%5B%5D=4&price_to=190000&area_from=55&has_bill_of_sale=true&has_mortgage=true&floor_first=false&floor_last=false&location_ids%5B%5D=8&location_ids%5B%5D=51&location_ids%5B%5D=2&location_ids%5B%5D=33&location_ids%5B%5D=54&location_ids%5B%5D=4&location_ids%5B%5D=52&location_ids%5B%5D=53&location_ids%5B%5D=1&location_ids%5B%5D=259&location_ids%5B%5D=314&location_ids%5B%5D=100&location_ids%5B%5D=99&location_ids%5B%5D=233&location_ids%5B%5D=68&location_ids%5B%5D=74&location_ids%5B%5D=69&location_ids%5B%5D=103&location_ids%5B%5D=246&location_ids%5B%5D=186&location_ids%5B%5D=376&location_ids%5B%5D=25&location_ids%5B%5D=138&location_ids%5B%5D=152&location_ids%5B%5D=26&location_ids%5B%5D=175&location_ids%5B%5D=135&location_ids%5B%5D=136&location_ids%5B%5D=16&location_ids%5B%5D=36"
))
PERSISTED_HASH = os.environ.get("BINA_PERSISTED_HASH",
    "872e9c694c34b6674514d48e9dcf1b46241d3d79f365ddf20d138f18e74554c5")

GRAPHQL_URL = "https://bina.az/graphql"
OPERATION = "SearchItems"
SORT = "BUMPED_AT_DESC"
PAGE_SIZE = 16
SCAN_PAGES = int(os.environ.get("SCAN_PAGES", "6"))
HTML_PAGES = int(os.environ.get("HTML_PAGES", "2"))   # search-page fallback depth
STATE_FILE = os.environ.get("STATE_FILE", "seen.json")
MAX_SEEN = 6000
SEND_PHOTOS = os.environ.get("SEND_PHOTOS", "true").lower() == "true"

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
API_HEADERS = {
    "User-Agent": BROWSER_UA, "Accept": "*/*",
    "Accept-Language": "az,en-US;q=0.9,en;q=0.8,ru;q=0.7",
    "Content-Type": "application/json",
    "Referer": "https://bina.az/alqi-satqi/menziller", "Origin": "https://bina.az",
}
HTML_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "az,en-US;q=0.9,en;q=0.8,ru;q=0.7",
}


def log(*args):
    print(*args, flush=True)


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
        log("Telegram sendMessage error:", e)
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
# Filter parsed from the search URL (used only for GraphQL results; the HTML
# page already applies the filter server-side)
# --------------------------------------------------------------------------- #
def build_filter(url):
    q = parse_qs(urlparse(url).query)

    def one(k):
        v = q.get(k)
        return v[0] if v else None

    def as_int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    def as_bool(v):
        return None if v is None else str(v).lower() in ("1", "true", "yes", "on")

    return {
        "room_ids": {as_int(v) for v in q.get("room_ids[]", []) if as_int(v) is not None},
        "location_ids": {as_int(v) for v in q.get("location_ids[]", []) if as_int(v) is not None},
        "price_from": as_int(one("price_from")), "price_to": as_int(one("price_to")),
        "area_from": float(one("area_from")) if one("area_from") else None,
        "area_to": float(one("area_to")) if one("area_to") else None,
        "has_bill_of_sale": as_bool(one("has_bill_of_sale")),
        "has_mortgage": as_bool(one("has_mortgage")),
        "not_first_floor": as_bool(one("floor_first")) is True,
        "not_last_floor": as_bool(one("floor_last")) is True,
    }


def matches(l, f):
    if f["room_ids"] and l.get("rooms") not in f["room_ids"]:
        return False
    price = l.get("price")
    if f["price_to"] is not None and (price is None or price > f["price_to"]):
        return False
    if f["price_from"] is not None and (price is None or price < f["price_from"]):
        return False
    area = l.get("area")
    if f["area_from"] is not None and (area is None or area < f["area_from"]):
        return False
    if f["area_to"] is not None and (area is None or area > f["area_to"]):
        return False
    if f["has_bill_of_sale"] is True and l.get("has_bill_of_sale") is not True:
        return False
    if f["has_mortgage"] is True and l.get("has_mortgage") is not True:
        return False
    if f["location_ids"]:
        lid = l.get("location_id")
        if lid is None or int(lid) not in f["location_ids"]:
            return False
    floor, floors = l.get("floor"), l.get("floors")
    if f["not_first_floor"] and floor is not None and floor <= 1:
        return False
    if f["not_last_floor"] and floor is not None and floors is not None and floor >= floors:
        return False
    return True


# --------------------------------------------------------------------------- #
# Source 1: GraphQL API
# --------------------------------------------------------------------------- #
def _params(cursor):
    variables = {"first": PAGE_SIZE, "filter": {"leased": False}, "sort": SORT}
    if cursor:
        variables["cursor"] = cursor
    return {
        "operationName": OPERATION,
        "variables": json.dumps(variables, separators=(",", ":")),
        "extensions": json.dumps(
            {"persistedQuery": {"version": 1, "sha256Hash": PERSISTED_HASH}},
            separators=(",", ":")),
    }


def _node_to_listing(node):
    def sub(k, fld):
        o = node.get(k)
        return o.get(fld) if isinstance(o, dict) else None

    photos = node.get("photos") or []
    photo = None
    if photos and isinstance(photos[0], dict):
        photo = photos[0].get("large") or photos[0].get("f460x345") or photos[0].get("thumbnail")

    area = sub("area", "value")
    try:
        area = float(area) if area is not None else None
    except (TypeError, ValueError):
        area = None
    price = sub("price", "value")
    try:
        price = int(float(price)) if price is not None else None
    except (TypeError, ValueError):
        price = None

    path = node.get("path")
    return {
        "id": int(node["id"]), "rooms": node.get("rooms"), "area": area,
        "area_units": sub("area", "units") or "m²", "floor": node.get("floor"),
        "floors": node.get("floors"), "price": price,
        "currency": sub("price", "currency") or "AZN", "location_id": sub("location", "id"),
        "location": sub("location", "fullName") or sub("location", "name") or sub("city", "name"),
        "has_bill_of_sale": node.get("hasBillOfSale"), "has_mortgage": node.get("hasMortgage"),
        "has_repair": node.get("hasRepair"), "updated_at": node.get("updatedAt"),
        "url": f"https://bina.az{path}" if path else f"https://bina.az/items/{node['id']}",
        "photo": photo, "html_only": False,
    }


def fetch_graphql():
    """Raise with a precise reason if the API can't be used; else return listings."""
    out, cursor = [], None
    for _ in range(SCAN_PAGES):
        r = requests.get(GRAPHQL_URL, params=_params(cursor), headers=API_HEADERS, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"GraphQL HTTP {r.status_code}")
        try:
            payload = r.json()
        except ValueError:
            raise RuntimeError("GraphQL did not return JSON (blocked or challenge page)")
        if payload.get("errors"):
            msg = "; ".join(str(e.get("message", e)) for e in payload["errors"])
            if "PersistedQueryNotFound" in msg or "PERSISTED_QUERY_NOT_FOUND" in msg:
                raise PersistedQueryError(msg)
            raise RuntimeError(f"GraphQL error: {msg}")
        conn = (payload.get("data") or {}).get("itemsConnection")
        if not conn:
            keys = ",".join((payload.get("data") or {}).keys()) or "none"
            raise RuntimeError(f"GraphQL: no itemsConnection (data keys: {keys})")
        for edge in conn.get("edges", []):
            node = edge.get("node")
            if node and node.get("id") is not None:
                try:
                    out.append(_node_to_listing(node))
                except Exception as e:
                    log("Skip bad node:", e)
        info = conn.get("pageInfo") or {}
        if not info.get("hasNextPage") or not info.get("endCursor"):
            break
        cursor = info["endCursor"]
    return out


# --------------------------------------------------------------------------- #
# Source 2: normal search web page (no signature needed; filters applied by site)
# --------------------------------------------------------------------------- #
def fetch_html():
    """Read the search results page(s) and extract listing ids + links."""
    found, seen_ids = [], set()
    base = BINA_SEARCH_URL
    for page in range(1, HTML_PAGES + 1):
        url = base if page == 1 else base + ("&" if "?" in base else "?") + f"page={page}"
        r = requests.get(url, headers=HTML_HEADERS, timeout=30)
        if r.status_code != 200:
            if page == 1:
                raise RuntimeError(f"search page HTTP {r.status_code}")
            break
        text = r.text
        matches_on_page = 0
        for m in re.finditer(r'/items/(\d+)(-[A-Za-z0-9\-]+)?', text):
            iid = m.group(1)
            if iid in seen_ids:
                continue
            seen_ids.add(iid)
            slug = m.group(2) or ""
            title = slug.lstrip("-").replace("-", " ").strip() or None
            found.append({
                "id": int(iid), "url": "https://bina.az" + m.group(0),
                "title": title, "html_only": True, "price": None, "rooms": None,
                "area": None, "area_units": "m²", "floor": None, "floors": None,
                "currency": "AZN", "location": None, "location_id": None,
                "has_bill_of_sale": None, "has_mortgage": None, "has_repair": None,
                "updated_at": None, "photo": None,
            })
            matches_on_page += 1
        if matches_on_page == 0 and page == 1:
            raise RuntimeError("search page had no listings (JS-only page or blocked)")
        if matches_on_page == 0:
            break
    return found


def fetch_listings():
    """Return (listings, source_label). Raise RuntimeError if both sources fail."""
    graphql_reason = None
    try:
        items = fetch_graphql()
        if items:
            return items, "API"
        graphql_reason = "API returned 0 listings"
    except PersistedQueryError as e:
        graphql_reason = f"API signature expired ({e})"
    except Exception as e:
        graphql_reason = f"API: {e}"

    log("Falling back to HTML page. Reason:", graphql_reason)
    try:
        items = fetch_html()
        return items, f"web page (API unavailable: {graphql_reason})"
    except Exception as e:
        raise RuntimeError(f"{graphql_reason}; web page also failed: {e}")


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        data.setdefault("listings", {})
        return data
    except Exception as e:
        log("Could not read state, starting fresh:", e)
        return {"listings": {}}


def save_state(state):
    listings = state["listings"]
    if len(listings) > MAX_SEEN:
        kept = sorted(listings.items(), key=lambda kv: kv[1].get("first_seen", ""), reverse=True)[:MAX_SEEN]
        state["listings"] = dict(kept)
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #
def _fmt_published(iso):
    if not iso:
        return None
    try:
        return dt.datetime.fromisoformat(str(iso)).strftime("%d %b %Y, %H:%M")
    except Exception:
        return str(iso)


def format_message(l):
    if l.get("html_only"):
        lines = ["🏠 <b>NEW APARTMENT FOUND</b>", ""]
        if l.get("title"):
            lines.append(f"📝 {html.escape(l['title'])}")
        lines.append("")
        lines.append(f'🔗 <a href="{html.escape(l["url"])}">Open listing</a>')
        lines.append("<i>(open the link for price, photos and full details)</i>")
        return "\n".join(lines)

    lines = ["🏠 <b>NEW APARTMENT FOUND</b>", ""]
    if l.get("price") is not None:
        lines.append(f"💰 <b>Price:</b> {l['price']:,} {l['currency']}")
    lines.append(f"🛏 <b>Rooms:</b> {l.get('rooms') if l.get('rooms') is not None else '-'}")
    if l.get("area") is not None:
        area = int(l["area"]) if float(l["area"]).is_integer() else l["area"]
        lines.append(f"📐 <b>Area:</b> {area} {l['area_units']}")
    floor = f"{l['floor']}/{l['floors']}" if l.get("floor") and l.get("floors") else (str(l.get("floor")) if l.get("floor") else "-")
    lines.append(f"🏢 <b>Floor:</b> {floor}")
    if l.get("location"):
        lines.append(f"📍 <b>Location:</b> {html.escape(str(l['location']))}")
    pub = _fmt_published(l.get("updated_at"))
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


def notify(l):
    text = format_message(l)
    if SEND_PHOTOS and l.get("photo"):
        return tg_send_photo(l["photo"], text)
    return tg_send_message(text)


# --------------------------------------------------------------------------- #
# Core
# --------------------------------------------------------------------------- #
def process(items, first_run, seen, source_is_html):
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    f = build_filter(BINA_SEARCH_URL)
    new_matches, seeded, notified = [], 0, 0

    for l in items:
        idk = str(l["id"])
        if idk in seen:
            continue
        # HTML results are already filtered by the website; API results we filter here.
        is_match = True if source_is_html else matches(l, f)
        if is_match:
            if first_run:
                seen[idk] = {"url": l["url"], "price": l.get("price"), "first_seen": now,
                             "notification_sent": True, "matched": True}
                seeded += 1
            else:
                new_matches.append(l)
        else:
            seen[idk] = {"first_seen": now, "notification_sent": True, "matched": False}

    if not first_run:
        for l in new_matches:
            if notify(l):
                seen[str(l["id"])] = {"url": l["url"], "price": l.get("price"), "first_seen": now,
                                      "notification_sent": True, "matched": True}
                notified += 1
                log("Notified:", l["id"], l.get("url"))
            else:
                log("Send failed, will retry next run:", l["id"])
    return seeded, notified


def main():
    if not BOT_TOKEN or not CHAT_ID:
        log("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set as secrets.")
        sys.exit(1)

    state = load_state()
    first_run = state is None
    if first_run:
        state = {"listings": {}}
    seen = state["listings"]

    try:
        items, source = fetch_listings()
    except PersistedQueryError:
        tg_send_message("⚠️ bina.az updated its site and the web-page fallback also failed. "
                        "Tell me and I'll adjust the reader.")
        return
    except Exception as e:
        if first_run:
            tg_send_message("🟡 <b>Bot is running</b>, but it could not read bina.az yet.\n"
                            f"Reason: {html.escape(str(e))}\n"
                            "It retries every 5 minutes. Send me this reason if it persists.")
        else:
            log("Fetch failed on later run:", e)
        return

    source_is_html = source.startswith("web page")

    if first_run:
        seeded, _ = process(items, True, seen, source_is_html)
        save_state(state)
        tg_send_message(f"✅ <b>Monitoring started</b> (via {html.escape(source)}).\n"
                        f"Recorded {seeded} current listing(s). From now on you'll get a "
                        f"message only when a brand-new matching apartment appears.")
        return

    _, notified = process(items, False, seen, source_is_html)
    save_state(state)
    log(f"Done via {source}. scanned={len(items)} notified={notified} seen_total={len(seen)}")


if __name__ == "__main__":
    main()
