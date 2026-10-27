#!/usr/bin/env python3
"""
Bamboo Housing watcher.

Scrapes each search URL in config.yaml, compares against listings already seen
(state/seen.json), and sends new ones to Discord and/or email.

Usage:
    python watcher.py                 # normal run
    python watcher.py --dry-run       # print what it finds, send nothing, save nothing
    python watcher.py --dump-html     # save fetched pages to debug/ (if parsing ever breaks)
    python watcher.py --test-notify   # send a fake listing to check Discord/email setup
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import smtplib
import sys
import time
from collections import Counter
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin

import requests
import yaml
from bs4 import BeautifulSoup, Tag

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state" / "seen.json"
DEBUG_DIR = ROOT / "debug"
BASE_URL = "https://bamboohousing.ca"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept-Language": "en-CA,en;q=0.9",
}
MAX_SEEN_PER_SEARCH = 1000

# ── Patterns ────────────────────────────────────────────────────────────────
PRICE_RE = re.compile(r"^\$\s*([\d,]+)\s*/\s*month$", re.I)        # "$900/month" exactly
ROOMS_RE = re.compile(r"(\d+)\s*Rooms?\s*of\s*(\d+)", re.I)          # "1 Room of 5"
TERM_RE = re.compile(r"\b(Winter|Spring|Summer|Fall)\b", re.I)
GENDER_RE = re.compile(r"(Female Only|Male Only|Coed)", re.I)
BADGES = {"featured", "university verified", "phone verified", "building", "verified"}

_NUM = r"(\d+|one|two|three|four|five|six)"
_WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
# "2 bed 1 bath", "5bed/5bath", "2B1B", "3 bedroom, 2 bathroom", "two bed two bath"
BED_BATH_RE = re.compile(
    _NUM + r"\s*(?:bed(?:room)?s?|br|b)(?![a-z])[\s/,&+\-]*(?:and\s+)?"
    + _NUM + r"\s*(?:bath(?:room)?s?|washrooms?|ba|b)(?![a-z])",
    re.I,
)
# "Bathrooms: 2", "2 Bathrooms", "Washrooms 3" (used on detail pages)
BATH_COUNT_RE = re.compile(
    r"(?:bath(?:room)?s?|washrooms?)\s*[:\-]?\s*(\d+)|(\d+)\s*(?:bath(?:room)?s?|washrooms?)\b",
    re.I,
)
PRIVATE_BATH_RE = re.compile(
    r"private\s+(?:bath(?:room)?|washroom|bed\s*(?:&|and)\s*bath)|en-?suite|own\s+(?:bath(?:room)?|washroom)",
    re.I,
)


@dataclass
class Listing:
    id: str
    price: int | None
    address: str
    title: str
    rooms_available: int | None = None
    bedrooms_total: int | None = None
    bathrooms: int | None = None
    private_bath: bool = False
    gender: str = ""
    term: str = ""
    url: str = ""
    image: str = ""
    extra_text: list[str] = field(default_factory=list)

    @property
    def bed_bath(self) -> str:
        beds = self.bedrooms_total
        if beds and self.bathrooms:
            ratio = beds / self.bathrooms
            r = f"{ratio:g}" if ratio == int(ratio) else f"{ratio:.2f}".rstrip("0")
            s = f"{beds} bed : {self.bathrooms} bath  ({r} bed per bath)"
            if self.private_bath:
                s += " · private bath"
            return s
        if self.private_bath:
            return f"{beds or '?'} bed unit · private/ensuite bathroom for your room"
        if beds:
            return f"{beds} bed unit · bathroom count not listed"
        return "not listed"

    @property
    def price_str(self) -> str:
        return f"${self.price:,}/month" if self.price else "price not listed"


# ── Fetching & parsing ──────────────────────────────────────────────────────
def fetch(url: str, session: requests.Session, retries: int = 3) -> str:
    last = None
    for attempt in range(retries):
        try:
            r = session.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last}")


def _is_price_el(el: Tag) -> bool:
    txt = el.get_text("", strip=True)
    return len(txt) < 25 and bool(PRICE_RE.match(txt))


def _clean_lines(el: Tag) -> list[str]:
    return [ln.strip() for ln in el.get_text("\n").split("\n") if ln.strip()]


def parse_listings(html: str, page_url: str, url_template: str | None = None) -> list[Listing]:
    """Find listing cards by locating "$X/month" price elements and growing each one
    out to the largest ancestor that contains only that one price."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()

    # innermost elements whose whole text is exactly a price
    price_els = [el for el in soup.find_all(True) if _is_price_el(el)]
    price_els = [el for el in price_els if not any(_is_price_el(c) for c in el.find_all(True))]

    counts: Counter = Counter()
    for pe in price_els:
        for anc in pe.parents:
            counts[id(anc)] += 1

    listings, seen_ids = [], set()
    for pe in price_els:
        card = pe
        while card.parent is not None and counts[id(card.parent)] == 1:
            card = card.parent

        text = card.get_text(" ", strip=True)
        if "starting from" in text.lower():          # company buildings, not sublets
            continue
        rooms = ROOMS_RE.search(text)
        if not rooms:                                # not a regular listing card
            continue

        lines = _clean_lines(card)
        price_txt = pe.get_text("", strip=True)
        pe_lines = _clean_lines(pe)                   # price may be split: ["$900", "/month"]
        n = len(pe_lines)
        start = next((k + n for k in range(len(lines)) if lines[k:k + n] == pe_lines), 0)
        after = [ln for ln in lines[start:] if ln.lower() not in BADGES]
        info_like = lambda ln: bool(  # noqa: E731
            ROOMS_RE.search(ln) or re.fullmatch(r"(Winter|Spring|Summer|Fall)", ln, re.I)
            or re.fullmatch(r"\d+\s*Months?", ln, re.I) or re.fullmatch(GENDER_RE.pattern, ln, re.I)
        )
        content = [ln for ln in after if not info_like(ln)]
        address = content[0] if content else ""
        title = content[1] if len(content) > 1 else ""

        gender = GENDER_RE.search(text)
        term = TERM_RE.search(" ".join(ln for ln in after if info_like(ln)) or text)

        # link + image
        href = ""
        a = card if card.name == "a" and card.get("href") else card.find("a", href=True)
        if a is None:
            a = card.find_parent("a", href=True)
        if a is not None:
            href = urljoin(BASE_URL, a["href"])
        img_el = card.find("img", src=re.compile(r"/listings/"))
        image = img_el["src"] if img_el else ""
        img_id = re.search(r"/listings/(\d+)\.", image)

        if href:
            lid = href
        elif img_id:
            lid = f"img:{img_id.group(1)}"
        else:
            lid = "h:" + hashlib.sha1(f"{address}|{title}".lower().encode()).hexdigest()[:16]
        if lid in seen_ids:
            continue
        seen_ids.add(lid)

        if not href and url_template and img_id:
            href = url_template.format(id=img_id.group(1))

        lst = Listing(
            id=lid,
            price=int(PRICE_RE.match(price_txt).group(1).replace(",", "")),
            address=address,
            title=title,
            rooms_available=int(rooms.group(1)),
            bedrooms_total=int(rooms.group(2)),
            gender=gender.group(1) if gender else "",
            term=term.group(1).title() if term else "",
            url=href or page_url,
            image=image,
        )
        apply_bed_bath(lst, [title])
        listings.append(lst)
    return listings


def _num(s: str) -> int:
    return int(s) if s.isdigit() else _WORDNUM[s.lower()]


def apply_bed_bath(lst: Listing, texts: list[str]) -> None:
    """Fill bedroom/bathroom info from free text (titles, descriptions)."""
    for t in texts:
        if not t:
            continue
        if lst.bathrooms is None:
            m = BED_BATH_RE.search(t)
            if m:
                beds, baths = _num(m.group(1)), _num(m.group(2))
                if 0 < baths <= 10 and 0 < beds <= 12:
                    lst.bathrooms = baths
                    lst.bedrooms_total = lst.bedrooms_total or beds
        if PRIVATE_BATH_RE.search(t):
            lst.private_bath = True


def enrich_from_detail(lst: Listing, session: requests.Session, search_url: str) -> None:
    """Open the listing page (if we have one) and look for a bathroom count."""
    if not lst.url or lst.url == search_url:
        return
    try:
        html = fetch(lst.url, session, retries=2)
    except RuntimeError:
        return
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    text = soup.get_text(" ", strip=True)
    apply_bed_bath(lst, [text])
    if lst.bathrooms is None:
        m = BATH_COUNT_RE.search(text)
        if m:
            n = int(m.group(1) or m.group(2))
            if 0 < n <= 10:
                lst.bathrooms = n


# ── Filters ─────────────────────────────────────────────────────────────────
def passes_filters(lst: Listing, search: dict) -> bool:
    if search.get("max_price") and lst.price and lst.price > search["max_price"]:
        return False
    if search.get("min_price") and lst.price and lst.price < search["min_price"]:
        return False
    hay = f"{lst.title} {lst.address} {lst.gender}".lower()
    return not any(k.lower() in hay for k in (search.get("exclude_keywords") or []))


# ── Notifiers ───────────────────────────────────────────────────────────────
def send_discord(webhook: str, search_name: str, listings: list[Listing], note: str = "") -> None:
    if not listings and note:
        requests.post(webhook, json={"content": note}, timeout=20).raise_for_status()
        return
    embeds = []
    for lst in listings:
        e = {
            "title": (lst.title or lst.address)[:250],
            "url": lst.url,
            "color": 0x00DF94,
            "fields": [
                {"name": "💰 Price", "value": lst.price_str, "inline": True},
                {"name": "📍 Address", "value": lst.address or "—", "inline": True},
                {"name": "🛏️ Bed : 🛁 Bath", "value": lst.bed_bath, "inline": False},
                {"name": "Rooms available", "value": f"{lst.rooms_available} of {lst.bedrooms_total}", "inline": True},
                {"name": "Who", "value": lst.gender or "—", "inline": True},
            ],
            "footer": {"text": f"Bamboo · {search_name}"},
        }
        if lst.image:
            e["thumbnail"] = {"url": lst.image}
        embeds.append(e)
    for i in range(0, len(embeds), 10):                       # Discord max 10 embeds/msg
        payload = {"embeds": embeds[i:i + 10]}
        if i == 0:
            payload["content"] = note or f"🏠 **{len(listings)} new listing(s)** for `{search_name}`"
        r = requests.post(webhook, json=payload, timeout=20)
        if r.status_code == 429:                              # rate limited → wait and retry once
            time.sleep(float(r.json().get("retry_after", 2)))
            r = requests.post(webhook, json=payload, timeout=20)
        r.raise_for_status()


def send_email(search_name: str, listings: list[Listing], note: str = "") -> None:
    host, port = os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", 587))
    user, pwd, to = os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"], os.environ["EMAIL_TO"]
    msg = EmailMessage()
    msg["Subject"] = note or f"[Bamboo] {len(listings)} new listing(s) – {search_name}"
    msg["From"], msg["To"] = user, to
    body = [note] if note else []
    html = [f"<p>{note}</p>"] if note else []
    for l in listings:
        body.append(f"{l.title}\n  {l.price_str} | {l.address}\n  Bed/Bath: {l.bed_bath}\n  {l.url}\n")
        html.append(
            f"<p><b><a href='{l.url}'>{l.title or l.address}</a></b><br>"
            f"💰 {l.price_str}<br>📍 {l.address}<br>🛏️/🛁 {l.bed_bath}<br>"
            f"Rooms: {l.rooms_available} of {l.bedrooms_total} · {l.gender}</p>"
        )
    msg.set_content("\n".join(body))
    msg.add_alternative("<html><body>" + "".join(html) + "</body></html>", subtype="html")
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls()
        s.login(user, pwd)
        s.send_message(msg)


def notify(cfg: dict, search_name: str, listings: list[Listing], note: str = "") -> None:
    n = cfg.get("notify", {})
    if n.get("discord", {}).get("enabled"):
        hook = os.environ.get(n["discord"].get("webhook_env", "DISCORD_WEBHOOK_URL"))
        if hook:
            send_discord(hook, search_name, listings, note)
        else:
            print("  ! Discord enabled but webhook env var not set", file=sys.stderr)
    if n.get("email", {}).get("enabled"):
        send_email(search_name, listings, note)


# ── State ───────────────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    for name, seen in state.get("seen", {}).items():
        if len(seen) > MAX_SEEN_PER_SEARCH:                   # keep newest N
            keep = sorted(seen.items(), key=lambda kv: kv[1], reverse=True)[:MAX_SEEN_PER_SEARCH]
            state["seen"][name] = dict(keep)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


# ── Main ────────────────────────────────────────────────────────────────────
def run(cfg: dict, dry_run: bool = False, dump_html: bool = False, html_override: str | None = None) -> int:
    state = load_state()
    state.setdefault("seen", {})
    state.setdefault("health", {})
    session = requests.Session()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    exit_code = 0

    for search in cfg.get("searches", []):
        if not search.get("enabled", True):
            continue
        name, url = search["name"], search["url"]
        print(f"\n▶ {name}")
        try:
            html = html_override if html_override is not None else fetch(url, session)
        except RuntimeError as e:
            print(f"  ! {e}", file=sys.stderr)
            exit_code = 1
            continue
        if dump_html:
            DEBUG_DIR.mkdir(exist_ok=True)
            (DEBUG_DIR / f"{name}.html").write_text(html)

        listings = parse_listings(html, url, cfg.get("listing_url_template"))
        print(f"  found {len(listings)} listings on page")

        # health check: warn once if the parser suddenly finds nothing
        was_ok = state["health"].get(name, True)
        if not listings:
            print("  ! 0 listings parsed — site layout may have changed. Run with --dump-html.", file=sys.stderr)
            if was_ok and not dry_run:
                notify(cfg, name, [], f"⚠️ Bamboo watcher found **0 listings** for `{name}`. "
                                      "The site layout may have changed — check the scraper.")
            state["health"][name] = False
            exit_code = 1
            continue
        state["health"][name] = True

        first_run = name not in state["seen"]
        seen = state["seen"].setdefault(name, {})
        new = [l for l in listings if l.id not in seen]
        for l in listings:
            seen.setdefault(l.id, now)

        if first_run and not cfg.get("notify_on_first_run", False):
            print(f"  first run: remembered {len(listings)} existing listings")
            if not dry_run:
                notify(cfg, name, [], f"👀 Now watching `{name}` — {len(listings)} existing listings saved. "
                                      "You'll be pinged when new ones show up.")
            continue

        new = [l for l in new if passes_filters(l, search)]
        if cfg.get("fetch_details", True) and html_override is None:
            for l in new:
                if l.bathrooms is None:
                    enrich_from_detail(l, session, url)
                    time.sleep(1)

        print(f"  {len(new)} new")
        for l in new:
            print(f"   • {l.price_str:>16} | {l.address} | {l.bed_bath} | {l.title}")
        if new and not dry_run:
            notify(cfg, name, new)

    if not dry_run:
        save_state(state)
    return exit_code


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--dry-run", action="store_true", help="print results; don't notify or save state")
    ap.add_argument("--dump-html", action="store_true", help="save fetched HTML to debug/")
    ap.add_argument("--from-file", help="parse a saved HTML file instead of fetching (for debugging)")
    ap.add_argument("--test-notify", action="store_true", help="send a sample alert to check your setup")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text())

    if args.test_notify:
        sample = Listing(
            id="test", price=950, address="200 University Ave W, Waterloo",
            title="TEST – Winter 2027 room in 3 bed 2 bath", rooms_available=1,
            bedrooms_total=3, bathrooms=2, gender="Coed", term="Winter", url=BASE_URL,
        )
        notify(cfg, "test", [sample], "🧪 Test alert from your Bamboo watcher")
        print("Sent test notification.")
        return

    html = Path(args.from_file).read_text() if args.from_file else None
    sys.exit(run(cfg, dry_run=args.dry_run or bool(args.from_file), dump_html=args.dump_html, html_override=html))


if __name__ == "__main__":
    main()
