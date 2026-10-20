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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import requests

ROOT = Path(__file__).parent
FEEDS_FILE = ROOT / "feeds.json"
STATE_FILE = ROOT / "seen.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
MAX_PER_RUN = 3     # por defecto; se puede cambiar por feed con "max_per_run" en feeds.json
MAX_SEEN = 600      # cuántas claves (id + enlace) recordar por feed
DESC_LEN = 350      # largo máximo de la descripción en el embed

TAG_RE = re.compile(r"<[^>]+>")
ALT_RE = re.compile(r"""<link[^>]+type=["']application/(?:rss|atom)\+xml["'][^>]*>""", re.I)
HREF_RE = re.compile(r"""href=["']([^"']+)["']""", re.I)
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


def norm_link(link):
    """Enlace sin #ancla ni parámetros de rastreo, para detectar repetidos."""
    if not link:
        return None
    p = urlsplit(link.strip())
    q = [(k, v) for k, v in parse_qsl(p.query)
         if not k.lower().startswith("utm_") and k.lower() not in ("fbclid", "gclid")]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), urlencode(q), ""))


def entry_keys(entry):
    """Un artículo se considera visto si coincide su id O su enlace."""
    keys = [entry.get("id"), norm_link(entry.get("link")), entry.get("title") if not entry.get("link") and not entry.get("id") else None]
    return [k for k in keys if k]


def build_embed(feed: dict, entry) -> dict:
    title = clean(entry.get("title", "(sin título)"))
    # Google News añade " - Nombre del medio" al título: lo quitamos
    src = (entry.get("source") or {}).get("title")
    if src and title.endswith(f" - {src}"):
        title = title[: -len(f" - {src}")].rstrip()
    title = truncate(title, 250)
    raw = entry.get("summary") or (entry.get("content") or [{}])[0].get("value", "")
    footer = feed["name"]
    if entry.get("author"):
        footer += f" · {clean(entry['author'])}"
    desc = truncate(clean(raw), DESC_LEN)
    # Si la descripción solo repite el título (típico de Google News), se omite
    if desc.lower().startswith(title.lower()[:60]) and len(desc) <= len(title) + 60:
        desc = ""
    embed = {
        "title": title,
        "color": int(feed.get("color", "#F5C518").lstrip("#"), 16),
        "footer": {"text": truncate(footer, 200)},
    }
    if desc:
        embed["description"] = desc
    if is_http(entry.get("link")):
        embed["url"] = entry["link"]
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        embed["timestamp"] = datetime.fromtimestamp(
            calendar.timegm(parsed), tz=timezone.utc
        ).isoformat()
    # Si el artículo no trae imagen, se usa la imagen fija del feed (por ejemplo, su logo)
    image = find_image(entry) or feed.get("fallback_image")
    if is_http(image):
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
        headers={
            "User-Agent": UA,
            "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        },
        timeout=30,
    )
    r.raise_for_status()
    parsed = feedparser.parse(r.content)
    parsed["_http"] = (
        f"HTTP {r.status_code}, {r.headers.get('Content-Type', '?')}, {len(r.content)} bytes"
    )
    parsed["_head"] = re.sub(r"\s+", " ", r.text[:200])
    title = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.S | re.I)
    parsed["_title"] = clean(title.group(1)) if title else ""
    alts = []
    for tag in ALT_RE.findall(r.text):
        m = HREF_RE.search(tag)
        if m:
            alts.append(m.group(1))
    parsed["_alt"] = alts[:6]
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
        parsed = None
        for attempt in range(3):
            try:
                parsed = fetch(url)
                break
            except Exception as exc:
                print(f"  ! intento {attempt + 1}/3 falló: {exc}")
                if attempt < 2:
                    time.sleep(5 * (attempt + 1))
        if parsed is None:
            print("  ! no se pudo leer el feed, se reintentará en la próxima ejecución")
            continue

        entries = [e for e in parsed.entries if entry_id(e)]

        if not entries:
            print(f"  ! el feed no devolvió artículos ({parsed.get('_http', '')})")
            if parsed.get("bozo"):
                print(f"    aviso del lector: {parsed.get('bozo_exception')}")
            if parsed.get("_title"):
                print(f"    título de la página recibida: {parsed['_title']}")
            if parsed.get("_alt"):
                print("    feeds que declara esa página: " + " | ".join(parsed["_alt"]))
            else:
                print(f"    la web respondió: {parsed.get('_head', '')}")
            continue

        if test:
            ok = post(webhook, build_embed(feed, entries[0]))
            print("  ✓ prueba enviada" if ok else "  ! la prueba falló")
            continue

        # Primera vez que vemos este feed: guardamos lo existente sin publicar nada
        if not state.get(url):
            keys = [k for e in reversed(entries) for k in entry_keys(e)]
            state[url] = keys[-MAX_SEEN:]
            print(f"  primera ejecución: {len(entries)} artículos marcados como vistos")
            continue

        seen = set(state[url])
        fresh, batch = [], set()
        for e in entries:
            keys = entry_keys(e)
            if any(k in seen or k in batch for k in keys):
                continue
            batch.update(keys)
            fresh.append(e)
        fresh.reverse()  # más antiguo primero

        limit = int(feed.get("max_per_run", MAX_PER_RUN))
        skipped, to_post = (fresh[:-limit], fresh[-limit:]) if limit > 0 else (fresh, [])
        done = [k for e in skipped for k in entry_keys(e)]
        if skipped:
            print(f"  (se omiten {len(skipped)} artículos antiguos por el límite de {limit})")

        for e in to_post:
            if post(webhook, build_embed(feed, e)):
                done.extend(entry_keys(e))
                print(f"  ✓ {clean(e.get('title', ''))[:70]}")
                time.sleep(1)

        state[url] = (state[url] + done)[-MAX_SEEN:]

    if not test:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
