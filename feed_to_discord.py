#!/usr/bin/env python3
"""Lee feeds RSS/Atom y publica los artículos nuevos en Discord como embeds."""
import calendar
import html
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests

ROOT = Path(__file__).parent
FEEDS_FILE = ROOT / "feeds.json"
STATE_FILE = ROOT / "seen.json"

UA = "Mozilla/5.0 (compatible; DiscordRSSBot/1.0)"
MAX_PER_RUN = 5     # máximo de artículos publicados por feed en cada ejecución
MAX_SEEN = 300      # cuántos IDs recordar por feed
DESC_LEN = 350      # largo máximo de la descripción en el embed

TAG_RE = re.compile(r"<[^>]+>")
IMG_RE = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.I)


def clean(text: str) -> str:
    text = re.sub(r"<(script|style).*?</\1>", "", text or "", flags=re.S | re.I)
    text = TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\s+([.,;:!?)])", r"\1", text)


def truncate(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return text[:n].rsplit(" ", 1)[0].rstrip(".,;:- ") + "…"


def is_http(url) -> bool:
    return isinstance(url, str) and url.startswith(("http://", "https://"))


def find_image(entry):
    for key in ("media_content", "media_thumbnail"):
        for m in entry.get(key, []) or []:
            kind = m.get("type", "") or m.get("medium", "")
            if kind.startswith(("video", "audio")):
                continue
            if is_http(m.get("url")):
                return m["url"]
    for enc in entry.get("enclosures", []) or []:
        if enc.get("type", "").startswith("image") and is_http(enc.get("href")):
            return enc["href"]
    for link in entry.get("links", []) or []:
        if link.get("rel") == "enclosure" and link.get("type", "").startswith("image"):
            if is_http(link.get("href")):
                return link["href"]
    blobs = [c.get("value", "") for c in entry.get("content", []) or []]
    blobs.append(entry.get("summary", ""))
    for blob in blobs:
        m = IMG_RE.search(blob or "")
        if m and is_http(m.group(1)):
            return m.group(1)
    return None


def entry_id(entry):
    return entry.get("id") or entry.get("link") or entry.get("title")


def build_embed(feed: dict, entry) -> dict:
    title = truncate(clean(entry.get("title", "(sin título)")), 250)
    raw = entry.get("summary") or (entry.get("content") or [{}])[0].get("value", "")
    footer = feed["name"]
    if entry.get("author"):
        footer += f" · {clean(entry['author'])}"
    embed = {
        "title": title,
        "description": truncate(clean(raw), DESC_LEN),
        "color": int(feed.get("color", "#F5C518").lstrip("#"), 16),
        "footer": {"text": truncate(footer, 200)},
    }
    if is_http(entry.get("link")):
        embed["url"] = entry["link"]
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        embed["timestamp"] = datetime.fromtimestamp(
            calendar.timegm(parsed), tz=timezone.utc
        ).isoformat()
    image = find_image(entry)
    if image:
        embed["image"] = {"url": image}
    return embed


def post(webhook: str, embed: dict) -> bool:
    for _ in range(4):
        try:
            r = requests.post(webhook, json={"embeds": [embed]}, timeout=20)
        except requests.RequestException as exc:
            print(f"  ! error de red al publicar: {exc}")
            return False
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        if r.ok:
            return True
        print(f"  ! Discord respondió {r.status_code}: {r.text[:200]}")
        return False
    return False


def fetch(url: str):
    r = requests.get(
        url,
        headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, */*"},
        timeout=30,
    )
    r.raise_for_status()
    parsed = feedparser.parse(r.content)
    parsed["_http"] = (
        f"HTTP {r.status_code}, {r.headers.get('Content-Type', '?')}, {len(r.content)} bytes"
    )
    parsed["_head"] = re.sub(r"\s+", " ", r.text[:200])
    return parsed


def main():
    feeds = json.loads(FEEDS_FILE.read_text(encoding="utf-8"))
    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    test = os.environ.get("TEST_MODE") == "1"  # modo prueba: no toca seen.json

    for feed in feeds:
        name, url = feed["name"], feed["url"]
        print(f"→ {name}")
        webhook = os.environ.get(feed["webhook_env"])
        if not webhook:
            print(f"  ! falta el secreto {feed['webhook_env']}, se omite")
            continue
        try:
            parsed = fetch(url)
        except Exception as exc:
            print(f"  ! no se pudo leer el feed: {exc}")
            continue

        entries = [e for e in parsed.entries if entry_id(e)]
        ids = [entry_id(e) for e in entries]

        if not entries:
            print(f"  ! el feed no devolvió artículos ({parsed.get('_http', '')})")
            if parsed.get("bozo"):
                print(f"    aviso del lector: {parsed.get('bozo_exception')}")
            print(f"    la web respondió: {parsed.get('_head', '')}")
            continue

        if test:
            ok = post(webhook, build_embed(feed, entries[0]))
            print("  ✓ prueba enviada" if ok else "  ! la prueba falló")
            continue

        # Primera vez que vemos este feed: guardamos lo existente sin publicar nada
        if not state.get(url):
            state[url] = ids[:MAX_SEEN]
            print(f"  primera ejecución: {len(ids)} artículos marcados como vistos")
            continue

        seen = set(state[url])
        fresh = [e for e in entries if entry_id(e) not in seen][::-1]  # más antiguo primero
        skipped, to_post = fresh[:-MAX_PER_RUN], fresh[-MAX_PER_RUN:]
        done = [entry_id(e) for e in skipped]

        for e in to_post:
            if post(webhook, build_embed(feed, e)):
                done.append(entry_id(e))
                print(f"  ✓ {clean(e.get('title', ''))[:70]}")
                time.sleep(1)

        state[url] = (state[url] + done)[-MAX_SEEN:]

    if not test:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
