#!/usr/bin/env python3
"""
Bina.az -> Telegram apartment monitor (current-API edition, Aug 2026).

Speaks bina.az's current GraphQL contract: server-side filters via the query
(roomIds, locationIds, priceTo, areaFrom, hasBillOfSale, hasMortgage, ...) and the
new ESItem response shape (price.total, preview.f460x345, string ids). It reads the
newest matching listings and messages you about brand-new ones, exactly once.
"""
import base64
import datetime as dt
import html
import json
import os
import sys
from urllib.parse import parse_qs, urlparse

import requests

# ---- The search you want to monitor (paste any bina.az search URL here) ----
BINA_SEARCH_URL = os.environ.get("BINA_SEARCH_URL", (
    "https://bina.az/baki/alqi-satqi/menziller?room_ids%5B%5D=2&room_ids%5B%5D=3&room_ids%5B%5D=4&price_to=190000&area_from=55&has_bill_of_sale=true&has_mortgage=true&floor_first=false&floor_last=false&location_ids%5B%5D=8&location_ids%5B%5D=51&location_ids%5B%5D=2&location_ids%5B%5D=33&location_ids%5B%5D=54&location_ids%5B%5D=4&location_ids%5B%5D=52&location_ids%5B%5D=53&location_ids%5B%5D=1&location_ids%5B%5D=259&location_ids%5B%5D=314&location_ids%5B%5D=100&location_ids%5B%5D=99&location_ids%5B%5D=233&location_ids%5B%5D=68&location_ids%5B%5D=74&location_ids%5B%5D=69&location_ids%5B%5D=103&location_ids%5B%5D=246&location_ids%5B%5D=186&location_ids%5B%5D=376&location_ids%5B%5D=25&location_ids%5B%5D=138&location_ids%5B%5D=152&location_ids%5B%5D=26&location_ids%5B%5D=175&location_ids%5B%5D=135&location_ids%5B%5D=136&location_ids%5B%5D=16&location_ids%5B%5D=36"
))
# Baku = "1", apartments-for-sale category = "1" (matches the /baki/alqi-satqi/menziller path).
CITY_ID = os.environ.get("BINA_CITY_ID", "1")
CATEGORY_ID = os.environ.get("BINA_CATEGORY_ID", "1")

# Current request signature used by bina.az's own website. If the bot ever reports
# it stopped working, capture a fresh one from the browser Network tab (see README).
PERSISTED_HASH = os.environ.get("BINA_PERSISTED_HASH",
    "b781511a943a4d710eefdf811a24dd4ae353e55d836952603ce0b37fde97d073")

GRAPHQL_URL = "https://bina.az/graphql"
OPERATION = "SearchItems"
SORT = "BUMPED_AT_DESC"
PAGE_SIZE = 16
SCAN_PAGES = int(os.environ.get("SCAN_PAGES", "6"))
STATE_FILE = os.environ.get("STATE_FILE", "seen.json")
MAX_SEEN = 6000
SEND_PHOTOS = os.environ.get("SEND_PHOTOS", "true").lower() == "true"

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# GitHub API persistence (set automatically by the workflow). If absent, falls
# back to a local file so you can still run it on a computer.
GH_TOKEN = os.environ.get("GH_TOKEN", "").strip()
GH_REPO = os.environ.get("GH_REPO", "").strip()
GH_BRANCH = os.environ.get("GH_BRANCH", "main").strip()
USE_API = bool(GH_TOKEN and GH_REPO)
IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"),
    "Accept": "*/*",
    "Accept-Language": "az,en-US;q=0.9,en;q=0.8,ru;q=0.7",
    "Content-Type": "application/json",
    "Referer": "https://bina.az/baki/alqi-satqi/menziller",
    "Origin": "https://bina.az",
    "x-platform": "desktop",
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
# Build the server-side GraphQL filter from your bina.az search URL
# --------------------------------------------------------------------------- #
def build_filter_variables(url):
    q = parse_qs(urlparse(url).query)

    def one(k):
        v = q.get(k)
        return v[0] if v else None

    def as_bool(v):
        return str(v).lower() in ("1", "true", "yes", "on") if v is not None else None

    def as_num(v):
        try:
            f = float(v)
            return int(f) if f.is_integer() else f
        except (TypeError, ValueError):
            return None

    filt = {"cityId": CITY_ID, "categoryId": CATEGORY_ID, "leased": False}

    rooms = [str(v) for v in q.get("room_ids[]", []) if str(v).strip()]
    if rooms:
        filt["roomIds"] = rooms
    locs = [str(v) for v in q.get("location_ids[]", []) if str(v).strip()]
    if locs:
        filt["locationIds"] = locs
    if as_num(one("price_to")) is not None:
        filt["priceTo"] = as_num(one("price_to"))
    if as_num(one("price_from")) is not None:
        filt["priceFrom"] = as_num(one("price_from"))
    if as_num(one("area_from")) is not None:
        filt["areaFrom"] = as_num(one("area_from"))
    if as_num(one("area_to")) is not None:
        filt["areaTo"] = as_num(one("area_to"))
    if as_bool(one("has_bill_of_sale")) is not None:
        filt["hasBillOfSale"] = as_bool(one("has_bill_of_sale"))
    if as_bool(one("has_mortgage")) is not None:
        filt["hasMortgage"] = as_bool(one("has_mortgage"))
    filt["floorFirst"] = as_bool(one("floor_first")) is True
    filt["floorLast"] = as_bool(one("floor_last")) is True
    return filt


# --------------------------------------------------------------------------- #
# GraphQL request + parsing (current ESItem shape)
# --------------------------------------------------------------------------- #
def _params(filter_vars, cursor):
    variables = {"first": PAGE_SIZE, "filter": filter_vars, "sort": SORT}
    if cursor:
        variables["cursor"] = cursor
    return {
        "operationName": OPERATION,
        "variables": json.dumps(variables, separators=(",", ":"), ensure_ascii=False),
        "extensions": json.dumps(
            {"persistedQuery": {"version": 1, "sha256Hash": PERSISTED_HASH}},
            separators=(",", ":")),
    }


def _node_to_listing(node):
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
    return {
        "id": int(node["id"]), "rooms": node.get("rooms"), "area": area,
        "area_units": sub("area", "units") or "m²", "floor": node.get("floor"),
        "floors": node.get("floors"), "price": price,
        "currency": sub("price", "currency") or "AZN", "location_id": loc_id,
        "location": sub("location", "fullName") or sub("location", "name") or sub("city", "name"),
        "has_bill_of_sale": node.get("hasBillOfSale"), "has_mortgage": node.get("hasMortgage"),
        "has_repair": node.get("hasRepair"), "updated_at": node.get("updatedAt"),
        "url": f"https://bina.az{path}" if path else f"https://bina.az/items/{node['id']}",
        "photo": photo,
    }


def fetch_listings():
    """Return newest matching listings. Raise with a clear reason on failure."""
    filter_vars = build_filter_variables(BINA_SEARCH_URL)
    out, cursor = [], None
    for _ in range(SCAN_PAGES):
        r = requests.get(GRAPHQL_URL, params=_params(filter_vars, cursor),
                         headers=HEADERS, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        try:
            payload = r.json()
        except ValueError:
            raise RuntimeError("response was not JSON (blocked or challenge page)")
        if payload.get("errors"):
            msg = "; ".join(str(e.get("message", e)) for e in payload["errors"])
            if "PersistedQueryNotFound" in msg or "PERSISTED_QUERY_NOT_FOUND" in msg:
                raise PersistedQueryError(msg)
            raise RuntimeError(f"GraphQL error: {msg}")
        conn = (payload.get("data") or {}).get("itemsConnection")
        if not conn:
            keys = ",".join((payload.get("data") or {}).keys()) or "none"
            raise RuntimeError(f"no itemsConnection (data keys: {keys})")
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
# Optional safety-net client-side filter (server already filters; this double-checks)
# --------------------------------------------------------------------------- #
def build_check_filter(url):
    q = parse_qs(urlparse(url).query)

    def one(k):
        v = q.get(k)
        return v[0] if v else None

    def as_int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    return {
        "room_ids": {as_int(v) for v in q.get("room_ids[]", []) if as_int(v) is not None},
        "location_ids": {as_int(v) for v in q.get("location_ids[]", []) if as_int(v) is not None},
        "price_to": as_int(one("price_to")),
        "area_from": float(one("area_from")) if one("area_from") else None,
    }


def passes_check(l, f):
    if f["room_ids"] and l.get("rooms") not in f["room_ids"]:
        return False
    if f["price_to"] is not None and (l.get("price") is None or l["price"] > f["price_to"]):
        return False
    if f["area_from"] is not None and (l.get("area") is None or l["area"] < f["area_from"]):
        return False
    if f["location_ids"]:
        lid = l.get("location_id")
        if lid is None or lid not in f["location_ids"]:
            return False
    return True


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def _gh_headers():
    return {"Authorization": f"Bearer {GH_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def _gh_url():
    return f"https://api.github.com/repos/{GH_REPO}/contents/{STATE_FILE}"


def load_state():
    """Return (state, sha, first_run). Reads the LATEST state (never stale)."""
    if not USE_API:  # local-file fallback (for running on a computer)
        if not os.path.exists(STATE_FILE):
            return {"listings": {}}, None, True
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        data.setdefault("listings", {})
        return data, None, False

    r = requests.get(_gh_url(), headers=_gh_headers(),
                     params={"ref": GH_BRANCH}, timeout=30)
    if r.status_code == 404:
        return {"listings": {}}, None, True     # first run: file doesn't exist yet
    r.raise_for_status()
    j = r.json()
    raw = base64.b64decode(j.get("content", "")).decode("utf-8") if j.get("content") else ""
    state = json.loads(raw) if raw.strip() else {"listings": {}}
    state.setdefault("listings", {})
    return state, j["sha"], False


def _prune(state):
    listings = state["listings"]
    if len(listings) > MAX_SEEN:
        kept = sorted(listings.items(), key=lambda kv: kv[1].get("first_seen", ""), reverse=True)[:MAX_SEEN]
        state["listings"] = dict(kept)


def save_state(state, sha):
    """Write state atomically. Returns (ok, reason). On conflict, merge + retry."""
    _prune(state)
    if not USE_API:
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=1)
            return True, "local file"
        except Exception as e:
            return False, f"local write error: {e}"

    reason = "unknown"
    for attempt in range(5):
        body = {"message": "Update seen listings", "branch": GH_BRANCH,
                "content": base64.b64encode(
                    json.dumps(state, ensure_ascii=False).encode("utf-8")).decode("ascii")}
        if sha:
            body["sha"] = sha
        r = requests.put(_gh_url(), headers=_gh_headers(), json=body, timeout=30)
        if r.status_code in (200, 201):
            return True, "saved"
        if r.status_code in (409, 422):  # sha stale -> someone else wrote; merge + retry
            reason = f"HTTP {r.status_code} (retrying)"
            g = requests.get(_gh_url(), headers=_gh_headers(),
                             params={"ref": GH_BRANCH}, timeout=30)
            if g.status_code == 200:
                j = g.json()
                sha = j["sha"]
                raw = base64.b64decode(j.get("content", "")).decode("utf-8") if j.get("content") else ""
                latest = json.loads(raw) if raw.strip() else {"listings": {}}
                latest.setdefault("listings", {})
                for k, v in state["listings"].items():   # union: never lose a seen id
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
def _fmt_published(iso):
    if not iso:
        return None
    try:
        return dt.datetime.fromisoformat(str(iso)).strftime("%d %b %Y, %H:%M")
    except Exception:
        return str(iso)


def format_message(l):
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
def process(items, first_run, seen):
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    check = build_check_filter(BINA_SEARCH_URL)
    new_matches, seeded, notified = [], 0, 0

    for l in items:
        idk = str(l["id"])
        if idk in seen:
            continue
        if not passes_check(l, check):
            seen[idk] = {"first_seen": now, "notification_sent": True, "matched": False}
            continue
        if first_run:
            seen[idk] = {"url": l["url"], "price": l.get("price"), "first_seen": now,
                         "notification_sent": True, "matched": True}
            seeded += 1
        else:
            new_matches.append(l)

    if not first_run:
        for l in new_matches:
            if notify(l):
                seen[str(l["id"])] = {"url": l["url"], "price": l.get("price"), "first_seen": now,
                                      "notification_sent": True, "matched": True}
                notified += 1
                log("Notified:", l["id"], l.get("price"), l.get("location"))
            else:
                log("Send failed, will retry next run:", l["id"])
    return seeded, notified


def main():
    if not BOT_TOKEN or not CHAT_ID:
        log("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set as secrets.")
        sys.exit(1)

    try:
        state, sha, first_run = load_state()
    except Exception as e:
        log("Could not load state; skipping this run to avoid duplicates:", e)
        if IN_ACTIONS:
            tg_send_message("⚠️ Bot could not READ its memory this run "
                            f"({html.escape(str(e))}). Skipping to avoid duplicates.")
        return
    seen = state["listings"]

    if IN_ACTIONS and not USE_API:
        tg_send_message("⚠️ Memory storage is not configured (GH_TOKEN / GH_REPO not "
                        "reaching the script), so nothing is being saved and listings "
                        "will repeat. Check the workflow 'env:' block.")

    try:
        items = fetch_listings()
    except PersistedQueryError:
        tg_send_message("⚠️ bina.az updated its site and the saved request signature expired. "
                        "Capture a fresh one from the browser Network tab (README) and update "
                        "BINA_PERSISTED_HASH. Monitoring is paused until then.")
        return
    except Exception as e:
        if first_run:
            tg_send_message("🟡 <b>Bot is running</b>, but it could not read bina.az yet.\n"
                            f"Reason: {html.escape(str(e))}\n"
                            "It retries every 5 minutes.")
        else:
            log("Fetch failed on later run:", e)
        return

    if first_run:
        seeded, _ = process(items, True, seen)
        ok, reason = save_state(state, sha)
        if ok:
            tg_send_message(f"✅ <b>Monitoring started.</b>\nScanned {len(items)} newest listings and "
                            f"recorded {seeded} matching your search. From now on you'll get a message "
                            f"only when a brand-new matching apartment appears.")
        else:
            tg_send_message(f"🟡 Seeded {seeded} listings but could NOT save memory.\n"
                            f"Reason: {html.escape(reason)}\nWill retry next run.")
        return

    _, notified = process(items, False, seen)
    ok, reason = save_state(state, sha)
    if not ok:
        tg_send_message("⚠️ Read bina fine, but could NOT save memory — so listings will "
                        f"repeat until this is fixed.\nReason: {html.escape(reason)}")
    log(f"Done. scanned={len(items)} notified={notified} saved={ok}({reason}) seen_total={len(seen)}")


if __name__ == "__main__":
    main()
