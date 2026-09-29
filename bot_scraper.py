import os
import json
import time
import csv
import random
import html as html_mod
import re
from datetime import datetime, timezone, date

# Транспорт для py_bandcamp (используется только в fallback)
os.environ["PYBANDCAMP_TRANSPORT"] = "curl_cffi"

import curl_cffi.requests as ccr

try:
    from py_bandcamp import BandCamp
    PYBANDCAMP_AVAILABLE = True
except ImportError:
    BandCamp = None
    PYBANDCAMP_AVAILABLE = False

# ==============================================================================
# НАСТРОЙКИ
# ==============================================================================
GENRES = [
    "ambient"
]

TARGET_ARTISTS_AND_LABELS = [
    # "https://sessionvictim.bandcamp.com"
]

MAX_DAYS_AGO = 2
ALLOW_UPCOMING = True

MAX_POSTS_PER_RUN = 5
POSTED_FILE = "posted_releases.json"
CSV_FILE = "releases_data.csv"

DELAY_BETWEEN_RELEASES = (1.0, 2.0)
DELAY_BETWEEN_PAGES = (2.0, 3.5)
MAX_PAGES_PER_ARTIST = 30
MAX_GENRE_RELEASES = 60

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@bc_ambient")

DEBUG = True

CF_TITLE_MARKERS = (
    "client challenge",
    "just a moment",
    "attention required",
    "access denied",
    "checking your browser",
    "please wait",
    "ddos protection",
)

# ==============================================================================
# HTTP-ЗАГОЛОВКИ
# ==============================================================================
def _headers_html():
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://bandcamp.com/",
    }


def _headers_json():
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://bandcamp.com/discover/ambient",
        "Origin": "https://bandcamp.com",
        "X-Requested-With": "XMLHttpRequest",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }


# ==============================================================================
# ФАЙЛЫ
# ==============================================================================
def init_csv_file():
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, mode="w", encoding="utf-8", newline="") as f:
            csv.writer(f).writerow([
                "published_at_utc", "release_date", "genre", "artist",
                "album_title", "url", "tags", "image_url"
            ])


def load_posted():
    if os.path.exists(POSTED_FILE):
        try:
            with open(POSTED_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def save_posted(posted_set):
    with open(POSTED_FILE, "w", encoding="utf-8") as f:
        json.dump(list(posted_set), f, ensure_ascii=False, indent=2)


def save_to_csv(details):
    published_at_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    rd = details.get("release_date")
    rd_str = rd.strftime("%Y-%m-%d") if rd else ""
    with open(CSV_FILE, mode="a", encoding="utf-8", newline="") as f:
        csv.writer(f).writerow([
            published_at_utc, rd_str,
            details.get("genre", "ambient"),
            details.get("artist", "Неизвестный артист").strip(),
            details.get("album_title", "Без названия").strip(),
            details.get("link", "").strip(),
            ", ".join(details.get("tags", [])),
            details.get("image", "").strip()
        ])


# ==============================================================================
# ХЕЛПЕРЫ
# ==============================================================================
def extract_subdomain(target):
    target = target.strip().lower()
    target = target.replace("https://", "").replace("http://", "")
    return target.split('.')[0].split('/')[0]


def _is_cf_title(t):
    if not t:
        return True
    low = t.lower().strip()
    return any(m in low for m in CF_TITLE_MARKERS)


def _strip_tags(text):
    if not text:
        return ""
    text = re.sub(r'<script[^>]*>.*?</script>', ' ', text, flags=re.I | re.S)
    text = re.sub(r'<style[^>]*>.*?</style>', ' ', text, flags=re.I | re.S)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html_mod.unescape(text)
    return re.sub(r'\s+', ' ', text).strip()


def _try_parse_date_string(d_str):
    if not d_str:
        return None
    s = str(d_str).strip()
    if not s:
        return None

    s = re.sub(r'[T\s]\d{1,2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+\-]\d{2}:?\d{2})?.*$', '', s).strip()
    s = s.rstrip('Z').strip().rstrip(',').strip()

    for fmt in ("%d %b %Y", "%d %B %Y", "%B %d, %Y", "%b %d, %Y",
                "%B %d %Y", "%b %d %Y", "%Y-%m-%d", "%Y/%m/%d",
                "%d.%m.%Y", "%d-%m-%Y", "%Y.%m.%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass

    for pat, fmts in (
        (r'(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})', ("%d %b %Y", "%d %B %Y")),
        (r'([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})', ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y")),
        (r'(\d{4}-\d{2}-\d{2})', ("%Y-%m-%d",)),
        (r'(\d{4}/\d{2}/\d{2})', ("%Y/%m/%d",)),
    ):
        m = re.search(pat, str(d_str))
        if m:
            for fmt in fmts:
                try:
                    return datetime.strptime(m.group(1), fmt).date()
                except ValueError:
                    pass
    return None


def _find_date_in_dict(d, keys=("release_date", "album_release_date",
                                "original_release_date", "publish_date",
                                "datePublished", "releaseDate")):
    if isinstance(d, dict):
        for k, v in d.items():
            if k in keys and v:
                parsed = _try_parse_date_string(str(v))
                if parsed:
                    return parsed
        for v in d.values():
            found = _find_date_in_dict(v, keys)
            if found:
                return found
    elif isinstance(d, list):
        for item in d:
            found = _find_date_in_dict(item, keys)
            if found:
                return found
    return None


def parse_date_from_html(html_text):
    if not html_text:
        return None

    m = re.search(r'data-tralbum=["\']([^"\']+)["\']', html_text)
    if m:
        try:
            tr = json.loads(html_mod.unescape(m.group(1)))
            parsed = _find_date_in_dict(tr)
            if parsed:
                return parsed
        except Exception:
            pass

    for ld_raw in re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html_text, re.I | re.S
    ):
        try:
            ld_data = json.loads(ld_raw.strip())
            items = ld_data if isinstance(ld_data, list) else [ld_data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                for key in ("datePublished", "releaseDate"):
                    if item.get(key):
                        parsed = _try_parse_date_string(str(item[key]))
                        if parsed:
                            return parsed
                graph = item.get("@graph")
                if isinstance(graph, list):
                    for g in graph:
                        if isinstance(g, dict):
                            for key in ("datePublished", "releaseDate"):
                                if g.get(key):
                                    parsed = _try_parse_date_string(str(g[key]))
                                    if parsed:
                                        return parsed
        except Exception:
            pass

    for dt in re.findall(r'<time[^>]*datetime=["\']([^"\']+)["\']', html_text, re.I):
        parsed = _try_parse_date_string(dt)
        if parsed:
            return parsed

    m = re.search(
        r'<meta\s+name=["\']bc-page-properties["\']\s+content=["\']([^"\']+)["\']',
        html_text, re.I
    )
    if m:
        try:
            pj = json.loads(html_mod.unescape(m.group(1)))
            parsed = _find_date_in_dict(pj)
            if parsed:
                return parsed
        except Exception:
            pass

    m = re.search(
        r'class=["\'][^"\']*tralbum-credits[^"\']*["\'][^>]*>(.*?)</div>',
        html_text, re.I | re.S
    )
    if m:
        bt = _strip_tags(m.group(1))
        dm = re.search(
            r'released?\s+(?:on\s+)?([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})',
            bt, re.I
        )
        if dm:
            parsed = _try_parse_date_string(dm.group(1))
            if parsed:
                return parsed

    for meta_re in (
        r'<meta\s+property=["\']og:description["\']\s+content=["\']([^"\']+)["\']',
        r'<meta\s+name=["\']description["\']\s+content=["\']([^"\']+)["\']',
    ):
        m = re.search(meta_re, html_text, re.I)
        if m:
            desc = html_mod.unescape(m.group(1))
            dm = re.search(
                r'released?\s+(?:on\s+)?([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})',
                desc, re.I
            )
            if dm:
                parsed = _try_parse_date_string(dm.group(1))
                if parsed:
                    return parsed

    text_only = _strip_tags(html_text)
    for pat in (
        r'released?\s+(?:on\s+)?([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})',
        r'released?\s+(?:on\s+)?(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})',
    ):
        m = re.search(pat, text_only, re.I)
        if m:
            parsed = _try_parse_date_string(m.group(1))
            if parsed:
                return parsed

    for pat in (
        r'itemprop=["\']datePublished["\'][^>]*content=["\']([^"\']+)["\']',
        r'content=["\']([^"\']+)["\'][^>]*itemprop=["\']datePublished["\']',
    ):
        m = re.search(pat, html_text, re.I)
        if m:
            parsed = _try_parse_date_string(m.group(1))
            if parsed:
                return parsed

    for raw in re.findall(
        r'(?:album_release_date|release_date|current_release_date|original_release_date)'
        r'"?\s*[:=]\s*"([^"]+)"',
        html_text, re.I
    ):
        parsed = _try_parse_date_string(raw)
        if parsed:
            return parsed

    return None


def _extract_og(html_text, prop):
    m = re.search(
        rf'<meta\s+property=["\']og:{prop}["\']\s+content=["\']([^"\']*)["\']',
        html_text, re.I
    )
    if m:
        return html_mod.unescape(m.group(1)).strip()
    return ""


def _title_from_tralbum(html_text):
    m = re.search(r'data-tralbum=["\']([^"\']+)["\']', html_text)
    if not m:
        return ""
    try:
        tr = json.loads(html_mod.unescape(m.group(1)))
    except Exception:
        return ""

    current = tr.get("current") or {}
    for key in ("title", "album_title"):
        if isinstance(current, dict) and current.get(key):
            return str(current[key]).strip()
    for key in ("album_title", "title"):
        if tr.get(key):
            return str(tr[key]).strip()
    return ""


def _title_from_url(url):
    try:
        slug = url.rstrip("/").split("/")[-1].split("?")[0]
        return slug.replace("-", " ").replace("_", " ").title()
    except Exception:
        return ""


def _extract_title(html_text, url=""):
    title = _title_from_tralbum(html_text)
    if title and not _is_cf_title(title):
        return title

    og_title = _extract_og(html_text, "title")
    if og_title and not _is_cf_title(og_title):
        return og_title.split("|")[0].strip()

    m = re.search(r'<title>([^<]+)</title>', html_text, re.I)
    if m:
        t = html_mod.unescape(m.group(1)).split("|")[0].strip()
        if t and not _is_cf_title(t):
            return t

    return _title_from_url(url)


def _extract_artist_from_page(html_text):
    a = _extract_og(html_text, "site_name")
    if a and not _is_cf_title(a):
        return a.split("|")[0].strip()
    return ""


# ==============================================================================
# ПАРСИНГ АРТИСТОВ / ЛЕЙБЛОВ (без изменений)
# ==============================================================================
def _collect_release_paths(subdomain, session):
    base = f"https://{subdomain}.bandcamp.com"
    all_paths = set()
    artist_name = subdomain

    for page in range(1, MAX_PAGES_PER_ARTIST + 1):
        url = f"{base}/music" if page == 1 else f"{base}/music?page={page}"
        print(f"   📄 Страница {page}: {url}")

        try:
            r = session.get(url, timeout=30)
        except Exception as e:
            print(f"      ⚠️ {e}")
            break

        if r.status_code != 200:
            print(f"      ⚠️ HTTP {r.status_code}")
            break

        html_text = r.text

        if page == 1:
            artist_name = _extract_artist_from_page(html_text) or subdomain
            if not artist_name or artist_name == subdomain:
                cm = re.search(r'data-client-items="([^"]+)"', html_text)
                if cm:
                    try:
                        items = json.loads(html_mod.unescape(cm.group(1)))
                        if items:
                            band = items[0].get("band_name") or items[0].get("band")
                            if band:
                                artist_name = band
                    except Exception:
                        pass
            print(f"   Артист: {artist_name}")

        new_paths = set()
        for m in re.finditer(
            r'(?:href="|"page_url"\s*:\s*"|"title_link"\s*:\s*")(/(?:album|track)/[a-zA-Z0-9_\-]+)',
            html_text
        ):
            path = m.group(1).split("?")[0]
            if path not in all_paths:
                new_paths.add(path)
                all_paths.add(path)

        print(f"      Новых путей: {len(new_paths)} (всего {len(all_paths)})")

        if not new_paths:
            print(f"      🏁 Новых путей нет, завершаем пагинацию.")
            break

        time.sleep(random.uniform(*DELAY_BETWEEN_PAGES))

    return all_paths, artist_name


def _try_pybandcamp_fallback(url):
    if not PYBANDCAMP_AVAILABLE:
        return None
    try:
        release = BandCamp.album_to_release(url, include_tracklist=False)
        raw = getattr(release, "release_date", None)
        if raw:
            return _try_parse_date_string(str(raw))
    except Exception:
        pass
    return None


def fetch_releases_from_artist(target):
    subdomain = extract_subdomain(target)
    if not subdomain:
        return []

    base = f"https://{subdomain}.bandcamp.com"
    print(f" 👤 [АРТИСТ/ЛЕЙБЛ]: {subdomain} ({base})...")

    releases = []

    with ccr.Session(impersonate="chrome120") as session:
        print(f"\n   ── Этап 1: сбор путей с /music ──")
        paths, artist_name = _collect_release_paths(subdomain, session)
        print(f"   Всего путей: {len(paths)}")

        if not paths:
            print("   ⚠️ Не удалось собрать ни одного релиза.")
            return []

        print(f"\n   ── Этап 2: парсинг страниц релизов ──")
        for i, path in enumerate(sorted(paths), 1):
            url = f"{base}{path}"
            print(f"\n   [{i}/{len(paths)}] {url}")

            try:
                r = session.get(url, timeout=30)
            except Exception as e:
                print(f"      ⚠️ {e}")
                continue

            if r.status_code != 200:
                print(f"      ⚠️ HTTP {r.status_code}")
                continue

            html_text = r.text
            release_date = parse_date_from_html(html_text)

            if not release_date:
                release_date = _try_pybandcamp_fallback(url)

            title = _extract_title(html_text, url=url)
            image = _extract_og(html_text, "image")
            page_artist = _extract_artist_from_page(html_text)
            credits_artist = page_artist or artist_name

            if DEBUG:
                print(f"      🔍 release_date = {release_date!r}, title = {title!r}")

            releases.append({
                "title_full": f"{title} by {credits_artist}",
                "artist": credits_artist,
                "album_title": title,
                "image": image,
                "description": f"New release from {credits_artist}.",
                "tags": [subdomain],
                "link": url,
                "genre": "artist/label",
                "release_date": release_date,
            })

            print(f"      ✅ {title} — {release_date or 'дата не найдена'}")

            time.sleep(random.uniform(*DELAY_BETWEEN_RELEASES))

    print(f"\n   ИТОГО релизов у {artist_name}: {len(releases)}")
    return releases


# ==============================================================================
# ПАРСИНГ ЖАНРОВ / ТЕГОВ — несколько стратегий
# ==============================================================================
def _try_get_web_api(genre, session):
    """Стратегия 1: старый GET-эндпоинт /api/discover/3/get_web."""
    print(f"   🔎 Стратегия 1: GET /api/discover/3/get_web")
    url = f"https://bandcamp.com/api/discover/3/get_web?p=0&s=new&g={genre}&f=all"
    try:
        r = session.get(url, headers=_headers_json(), timeout=20)
        print(f"      HTTP {r.status_code}, len={len(r.text)}")
        if r.status_code == 200:
            try:
                data = r.json()
                items = data.get("items", [])
                print(f"      items: {len(items)}")
                if items:
                    return items
            except Exception as e:
                print(f"      ⚠️ JSON: {e}, первые 200 символов: {r.text[:200]}")
    except Exception as e:
        print(f"      ⚠️ {e}")
    return []


def _try_discover_web_api(genre, session):
    """Стратегия 2: POST /api/discover/1/discover_web с разными payload."""
    print(f"   🔎 Стратегия 2: POST /api/discover/1/discover_web")

    payloads = [
        # Вариант A: filters как объект (как у нас было)
        {
            "filters": {
                "format": "all",
                "genre": genre,
                "location": 0,
                "sort": "new",
                "tag_slug": genre,
            },
            "page": 1,
        },
        # Вариант B: filters как список
        {
            "filters": [
                {"field": "genre", "value": genre},
                {"field": "sort", "value": "new"},
            ],
            "page": 1,
        },
        # Вариант C: параметры в корне
        {
            "genre": genre,
            "sort": "new",
            "page": 1,
        },
    ]

    for idx, payload in enumerate(payloads):
        print(f"      Пробуем payload #{idx+1}: {json.dumps(payload)[:120]}...")
        try:
            r = session.post(
                "https://bandcamp.com/api/discover/1/discover_web",
                json=payload,
                headers={**_headers_json(), "Content-Type": "application/json"},
                timeout=20,
            )
            print(f"      HTTP {r.status_code}, len={len(r.text)}")
            if r.status_code == 200:
                try:
                    data = r.json()
                    items = (
                        data.get("items")
                        or data.get("results")
                        or data.get("cards")
                        or []
                    )
                    print(f"      items: {len(items)}")
                    if items:
                        return items
                except Exception as e:
                    print(f"      ⚠️ JSON: {e}, первые 200 символов: {r.text[:200]}")
            else:
                print(f"      первые 200 символов: {r.text[:200]}")
        except Exception as e:
            print(f"      ⚠️ {e}")
    return []


def _try_html_discover(genre, session):
    """Стратегия 3: HTML-страница /discover/<genre> + data-blob."""
    print(f"   🔎 Стратегия 3: HTML /discover/{genre}")
    url = f"https://bandcamp.com/discover/{genre}?s=new&p=digital"
    try:
        r = session.get(url, headers=_headers_html(), timeout=30)
        print(f"      HTTP {r.status_code}, len={len(r.text)}")
        if r.status_code != 200:
            return []

        html_text = r.text

        # 3.1 data-blob
        blob_m = re.search(r'data-blob="([^"]+)"', html_text)
        if blob_m:
            try:
                blob = json.loads(html_mod.unescape(blob_m.group(1)))
                items = (
                    blob.get("hub_data", {}).get("dig_deeper", {}).get("items", [])
                    or blob.get("tab_data", {}).get("dig_deeper", {}).get("items", [])
                    or blob.get("dig_deeper", {}).get("items", [])
                    or blob.get("items", [])
                )
                print(f"      data-blob items: {len(items)}")
                if items:
                    return items
            except Exception as e:
                print(f"      ⚠️ data-blob: {e}")

        # 3.2 Regex по ссылкам
        links = re.findall(
            r'https://[a-zA-Z0-9\-_]+\.bandcamp\.com/(?:album|track)/[a-zA-Z0-9\-_]+',
            html_text
        )
        print(f"      regex links: {len(links)}")
        if links:
            # Преобразуем в items-подобную структуру
            return [{"tralbum_url": l} for l in links]
    except Exception as e:
        print(f"      ⚠️ {e}")
    return []


def _collect_genre_candidates(genre, session):
    """Пробует все стратегии по очереди и возвращает первого, кто дал результат."""
    strategies = [
        ("get_web", _try_get_web_api),
        ("discover_web", _try_discover_web_api),
        ("html_discover", _try_html_discover),
    ]
    for name, fn in strategies:
        print(f"\n   ── Стратегия: {name} ──")
        items = fn(genre, session)
        if items:
            candidates = []
            seen = set()
            for it in items:
                link = (
                    it.get("tralbum_url")
                    or it.get("url")
                    or it.get("page_url")
                    or it.get("link")
                )
                if not link:
                    continue
                link = link.split("?")[0]
                if link in seen:
                    continue
                seen.add(link)
                candidates.append({
                    "link": link,
                    "title": (
                        it.get("album_title")
                        or it.get("title")
                        or it.get("primary_text")
                        or ""
                    ),
                    "artist": (
                        it.get("band_name")
                        or it.get("artist")
                        or it.get("secondary_text")
                        or ""
                    ),
                    "image": (
                        it.get("album_art")
                        or it.get("image")
                        or ""
                    ),
                })
            print(f"      ✅ {name}: {len(candidates)} кандидатов")
            return candidates[:MAX_GENRE_RELEASES]
        else:
            print(f"      ❌ {name}: 0 кандидатов")
    return []


def fetch_releases_from_genre(genre):
    genre_clean = genre.strip().lower().replace(" ", "-")
    if not genre_clean:
        return []

    print(f" 🏷️ [ЖАНР]: {genre_clean}...")
    releases = []
    seen_links = set()

    with ccr.Session(impersonate="chrome120") as session:
        print(f"\n   ── Этап 1: сбор кандидатов ──")
        candidates = _collect_genre_candidates(genre_clean, session)
        print(f"   Итого кандидатов: {len(candidates)}")

        if not candidates:
            print("   ⚠️ Не удалось собрать ни одного кандидата.")
            return []

        print(f"\n   ── Этап 2: парсинг страниц релизов ──")
        for i, cand in enumerate(candidates, 1):
            url = cand["link"]
            if url in seen_links:
                continue
            seen_links.add(url)

            print(f"\n   [{i}/{len(candidates)}] {url}")

            try:
                r = session.get(url, headers=_headers_html(), timeout=30)
            except Exception as e:
                print(f"      ⚠️ {e}")
                continue

            if r.status_code != 200:
                print(f"      ⚠️ HTTP {r.status_code}")
                continue

            html_text = r.text
            release_date = parse_date_from_html(html_text)
            if not release_date:
                release_date = _try_pybandcamp_fallback(url)

            title = _extract_title(html_text, url=url) or cand.get("title") or ""
            image = _extract_og(html_text, "image") or cand.get("image") or ""
            artist = (
                _extract_artist_from_page(html_text)
                or cand.get("artist")
                or "Various Artists"
            )

            if DEBUG:
                print(f"      🔍 release_date = {release_date!r}, title = {title!r}")

            releases.append({
                "title_full": f"{title} by {artist}",
                "artist": artist,
                "album_title": title,
                "image": image,
                "description": f"New release in #{genre_clean}.",
                "tags": [genre_clean],
                "link": url,
                "genre": genre_clean,
                "release_date": release_date,
            })

            print(f"      ✅ {title} — {release_date or 'дата не найдена'}")

            time.sleep(random.uniform(*DELAY_BETWEEN_RELEASES))

    print(f"\n   ИТОГО релизов в жанре '{genre_clean}': {len(releases)}")
    return releases


def is_release_date_valid(release_date):
    if release_date is None:
        print("   ⚠️ Дата релиза не найдена, пропуск.")
        return False

    today = datetime.now(timezone.utc).date()
    days_diff = (today - release_date).days

    if days_diff < 0:
        is_ok = ALLOW_UPCOMING
        print(f"   📅 Дата релиза: {release_date} "
              f"({'Анонс / Предзаказ' if is_ok else 'Пропущен (предзаказ)'})")
        return is_ok

    is_ok = days_diff <= MAX_DAYS_AGO
    status = f"{days_diff} дн. назад" if is_ok else f"Устарел ({days_diff} дн. назад)"
    print(f"   📅 Дата релиза: {release_date} ({status})")
    return is_ok


# ==============================================================================
# TELEGRAM
# ==============================================================================
def send_to_telegram(release):
    import requests as req

    if not TELEGRAM_BOT_TOKEN:
        print(" Ошибка: TELEGRAM_BOT_TOKEN не задан")
        return False

    title = html_mod.escape(release["title_full"])
    desc = html_mod.escape(release["description"])

    tags_list = [f"#{t.lower().replace('-', '_').replace(' ', '_')}" for t in release["tags"]]
    main_hashtag = f"#{release['genre'].lower().replace('-', '_').replace(' ', '_')}"
    if main_hashtag not in tags_list and release['genre'] != "artist/label":
        tags_list.insert(0, main_hashtag)
    tags_str = " ".join(tags_list)

    rd = release.get("release_date")
    date_line = f"📅 Дата релиза: <b>{rd.strftime('%d %b %Y')}</b>\n\n" if rd else ""

    caption = (
        f"🌌 <b>{title}</b>\n\n"
        f"{date_line}"
        f"📝 <i>{desc}</i>\n\n"
        f"🏷️ {tags_str}\n\n"
        f"🔗 <a href=\"{release['link']}\">Слушать / Купить на Bandcamp</a>"
    )

    if release.get("image"):
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "photo": release["image"],
                   "caption": caption, "parse_mode": "HTML"}
    else:
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": caption,
                   "parse_mode": "HTML", "disable_web_page_preview": False}

    try:
        return req.post(api_url, data=payload, timeout=20).ok
    except Exception as e:
        print(f"❌ Telegram: {e}")
        return False


# ==============================================================================
# MAIN
# ==============================================================================
def main():
    if not TELEGRAM_BOT_TOKEN:
        print("Ошибка: Не задан TELEGRAM_BOT_TOKEN")
        return

    print("✅ Скрипт запущен (curl_cffi + HTML-парсинг дат)")

    init_csv_file()
    posted = load_posted()

    releases = []
    seen_links = set()

    for target in TARGET_ARTISTS_AND_LABELS:
        if target.strip():
            for r in fetch_releases_from_artist(target):
                if r["link"] not in seen_links:
                    seen_links.add(r["link"])
                    releases.append(r)

    for genre in GENRES:
        if genre.strip():
            for r in fetch_releases_from_genre(genre):
                if r["link"] not in seen_links:
                    seen_links.add(r["link"])
                    releases.append(r)

    print(f"\n🔎 Всего найдено релизов: {len(releases)}")

    new_releases = [r for r in releases if r["link"] not in posted]
    print(f"✨ Новых (не публиковались): {len(new_releases)}")

    if not new_releases:
        print("🏁 Нет новых релизов для обработки.")
        return

    new_releases.sort(
        key=lambda x: x["release_date"] if x["release_date"] is not None else date.min,
        reverse=True
    )

    print("\n📊 Итоговый порядок:")
    for r in new_releases:
        d = r['release_date'].strftime('%Y-%m-%d') if r['release_date'] else 'Дата неизвестна'
        print(f"   • {d} — {r['title_full']}")

    new_posts = 0
    for release in new_releases:
        if new_posts >= MAX_POSTS_PER_RUN:
            print(f"\n🛑 Лимит {MAX_POSTS_PER_RUN} постов за запуск.")
            break

        print(f"\n🔍 Оценка: {release['title_full']}")
        if not is_release_date_valid(release.get("release_date")):
            continue

        print(f"🚀 Публикация: {release['title_full']}")
        if send_to_telegram(release):
            posted.add(release["link"])
            save_to_csv(release)
            new_posts += 1
            time.sleep(3)
        else:
            print(f"❌ Не удалось отправить: {release['link']}")

    save_posted(posted)
    print(f"\n🏁 Завершено. Опубликовано: {new_posts}")


if __name__ == "__main__":
    main()
