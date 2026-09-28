import os
import json
import html
import time
import csv
import re
import random
import urllib.parse
from datetime import datetime, timezone, date
import requests

try:
    from curl_cffi import requests as curl_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False

# ==============================================================================
# НАСТРОЙКИ
# ==============================================================================
GENRES = [
    # "ambient"
]

TARGET_ARTISTS_AND_LABELS = [
    "https://sessionvictim.bandcamp.com"
]

MAX_DAYS_AGO = 30
ALLOW_UPCOMING = True

MAX_POSTS_PER_RUN = 5
POSTED_FILE = "posted_releases.json"
CSV_FILE = "releases_data.csv"

DEBUG_HTML = True                     # сохранять HTML неудачных страниц в debug_html/
DEBUG_HTML_DIR = "debug_html"
FETCH_RETRIES = 3                     # попыток на один URL

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@bc_ambient")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]


def get_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }


def is_valid_html_response(text):
    """HTML считается валидным, только если содержит реальные маркеры страницы Bandcamp."""
    if not text or len(text) < 500:
        return False
    low = text.lower()
    anti_bot = (
        "just a moment", "enable javascript", "attention required",
        "<title>access denied</title>", "cf-browser-verification",
        "checking your browser", "please verify you are a human",
        "ddos protection", "ray id",
    )
    if any(x in low for x in anti_bot):
        return False
    # Должен быть хотя бы один признак реальной страницы Bandcamp
    bandcamp_markers = (
        "data-tralbum", "bc-page-properties", "bcbits.com",
        "bandcamp.com/", "tralbum-credits",
    )
    if not any(m in low for m in bandcamp_markers):
        return False
    return True


def is_valid_json_response(text):
    if not text or len(text) < 10:
        return False
    try:
        json.loads(text)
        return True
    except Exception:
        return False


def _dump_html(url, text):
    if not DEBUG_HTML:
        return
    try:
        os.makedirs(DEBUG_HTML_DIR, exist_ok=True)
        # имя файла из URL
        safe = re.sub(r'[^a-zA-Z0-9]+', '_', url)[-120:]
        path = os.path.join(DEBUG_HTML_DIR, f"{safe}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text or "")
    except Exception:
        pass


# ==============================================================================
# РАБОТА С ФАЙЛАМИ
# ==============================================================================
def init_csv_file():
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, mode="w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "published_at_utc",
                "release_date",
                "genre",
                "artist",
                "album_title",
                "url",
                "tags",
                "image_url"
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
    release_date = details.get("release_date")
    release_date_str = release_date.strftime("%Y-%m-%d") if release_date else ""
    genre = details.get("genre", "ambient")
    artist = details.get("artist", "Неизвестный артист").strip()
    album_title = details.get("album_title", "Без названия").strip()
    url = details.get("link", "").strip()
    tags = ", ".join(details.get("tags", []))
    image_url = details.get("image", "").strip()

    with open(CSV_FILE, mode="a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            published_at_utc, release_date_str, genre, artist,
            album_title, url, tags, image_url
        ])


# ==============================================================================
# ПАРСИНГ ДАТЫ
# ==============================================================================
def _strip_tags(text):
    if not text:
        return ""
    text = re.sub(r'<script[^>]*>.*?</script>', ' ', text, flags=re.I | re.S)
    text = re.sub(r'<style[^>]*>.*?</style>', ' ', text, flags=re.I | re.S)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html.unescape(text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _try_parse_date_string(d_str):
    if not d_str:
        return None

    s = str(d_str).strip()
    if not s:
        return None

    s = re.sub(
        r'[T\s]\d{1,2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+\-]\d{2}:?\d{2})?.*$',
        '', s
    ).strip()
    s = s.rstrip('Z').strip().rstrip(',').strip()

    formats = (
        "%d %b %Y", "%d %B %Y",
        "%B %d, %Y", "%b %d, %Y",
        "%B %d %Y", "%b %d %Y",
        "%Y-%m-%d", "%Y/%m/%d",
        "%d.%m.%Y", "%d-%m-%Y",
        "%Y.%m.%d",
    )
    for fmt in formats:
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

    # 1. data-tralbum
    tralbum_match = re.search(r'data-tralbum=["\']([^"\']+)["\']', html_text)
    if tralbum_match:
        try:
            tr_json = json.loads(html.unescape(tralbum_match.group(1)))
            parsed = _find_date_in_dict(tr_json)
            if parsed:
                return parsed
        except Exception:
            pass

    # 2. JSON-LD
    ld_matches = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html_text, re.I | re.S
    )
    for ld_raw in ld_matches:
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

    # 3. <time datetime="...">
    for dt in re.findall(r'<time[^>]*datetime=["\']([^"\']+)["\']', html_text, re.I):
        parsed = _try_parse_date_string(dt)
        if parsed:
            return parsed

    # 4. bc-page-properties
    props = re.search(
        r'<meta\s+name=["\']bc-page-properties["\']\s+content=["\']([^"\']+)["\']',
        html_text, re.I
    )
    if props:
        try:
            props_json = json.loads(html.unescape(props.group(1)))
            parsed = _find_date_in_dict(props_json)
            if parsed:
                return parsed
        except Exception:
            pass

    # 5. tralbum-credits
    credits = re.search(
        r'class=["\'][^"\']*tralbum-credits[^"\']*["\'][^>]*>(.*?)</div>',
        html_text, re.I | re.S
    )
    if credits:
        block_text = _strip_tags(credits.group(1))
        m = re.search(
            r'released?\s+(?:on\s+)?([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})',
            block_text, re.I
        )
        if m:
            parsed = _try_parse_date_string(m.group(1))
            if parsed:
                return parsed

    # 6. og:description / meta description
    for meta_re in (
        r'<meta\s+property=["\']og:description["\']\s+content=["\']([^"\']+)["\']',
        r'<meta\s+name=["\']description["\']\s+content=["\']([^"\']+)["\']',
    ):
        m = re.search(meta_re, html_text, re.I)
        if m:
            desc = html.unescape(m.group(1))
            dm = re.search(
                r'released?\s+(?:on\s+)?([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})',
                desc, re.I
            )
            if dm:
                parsed = _try_parse_date_string(dm.group(1))
                if parsed:
                    return parsed

    # 7. Общий regex по тексту
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

    # 8. itemprop datePublished
    for pat in (
        r'itemprop=["\']datePublished["\'][^>]*content=["\']([^"\']+)["\']',
        r'content=["\']([^"\']+)["\'][^>]*itemprop=["\']datePublished["\']',
    ):
        m = re.search(pat, html_text, re.I)
        if m:
            parsed = _try_parse_date_string(m.group(1))
            if parsed:
                return parsed

    # 9. JS-переменные
    for raw in re.findall(
        r'(?:album_release_date|release_date|current_release_date|original_release_date)'
        r'"?\s*[:=]\s*"([^"]+)"',
        html_text, re.I
    ):
        parsed = _try_parse_date_string(raw)
        if parsed:
            return parsed

    return None


# ==============================================================================
# МОБИЛЬНОЕ API BANDCAMP (надёжнее HTML)
# ==============================================================================
def fetch_release_date_via_api(tralbum_id, band_id=None, band_name=None, tralbum_type="a"):
    """Дата релиза через мобильное API Bandcamp. Работает без HTML."""
    if not tralbum_id:
        return None

    # Пробуем разные комбинации параметров
    candidates = []
    if band_id:
        candidates.append(
            f"https://bandcamp.com/api/mobile/24/tralbum_details"
            f"?band_id={band_id}&tralbum_id={tralbum_id}&tralbum_type={tralbum_type}"
        )
    if band_name:
        candidates.append(
            f"https://bandcamp.com/api/mobile/24/tralbum_details"
            f"?band_name={urllib.parse.quote(band_name)}&tralbum_id={tralbum_id}&tralbum_type={tralbum_type}"
        )
    candidates.append(
        f"https://bandcamp.com/api/mobile/24/tralbum_details"
        f"?tralbum_id={tralbum_id}&tralbum_type={tralbum_type}"
    )

    for url in candidates:
        try:
            r = requests.get(url, headers=get_headers(), timeout=15)
            if r.status_code != 200 or not is_valid_json_response(r.text):
                continue
            data = r.json()
            parsed = _find_date_in_dict(data)
            if parsed:
                return parsed
        except Exception:
            continue
    return None


# ==============================================================================
# ЗАГРУЗКА HTML С RETRY
# ==============================================================================
def fetch_html(url, referer=None, timeout=30, retries=FETCH_RETRIES):
    headers = get_headers()
    if referer:
        headers["Referer"] = referer

    last_status = None
    last_len = 0

    for attempt in range(1, retries + 1):
        # --- curl_cffi ---
        if CURL_CFFI_AVAILABLE:
            try:
                res = curl_requests.get(
                    url, headers=headers, impersonate="chrome120", timeout=timeout
                )
                last_status = res.status_code
                last_len = len(res.text or "")
                if res.status_code == 200 and is_valid_html_response(res.text):
                    return res.text
            except Exception as e:
                print(f"   ⚠️ curl_cffi попытка {attempt}: {e}")

        # --- requests ---
        try:
            res = requests.get(url, headers=headers, timeout=timeout)
            last_status = res.status_code
            last_len = len(res.text or "")
            if res.status_code == 200 and is_valid_html_response(res.text):
                return res.text
        except Exception as e:
            print(f"   ⚠️ requests попытка {attempt}: {e}")

        if attempt < retries:
            pause = 2 ** attempt + random.uniform(0, 1)
            print(f"   ↻ Повтор через {pause:.1f}с (status={last_status}, len={last_len})")
            time.sleep(pause)

    print(f"   ❌ HTML не получен после {retries} попыток (last status={last_status}, len={last_len})")
    if DEBUG_HTML:
        _dump_html(url, "")  # сохраняем пустой файл как маркер, что страница не отдалась
    return ""


def fetch_release_date(release):
    """Принимает словарь релиза. Возвращает date или None."""
    url = release.get("link")
    tralbum_id = release.get("tralbum_id")
    band_id = release.get("band_id")
    band_name = release.get("band_name")

    # 1. Пробуем мобильное API (если есть ID)
    if tralbum_id:
        parsed = fetch_release_date_via_api(
            tralbum_id, band_id=band_id, band_name=band_name
        )
        if parsed:
            return parsed

    # 2. Парсим HTML
    html_text = fetch_html(url)
    if html_text:
        parsed = parse_date_from_html(html_text)
        if parsed:
            return parsed

        # Fallback: если на HTML-странице нашли ID, но не нашли дату — пробуем API
        m = re.search(r'data-tralbum=["\']([^"\']+)["\']', html_text)
        if m:
            try:
                tr_json = json.loads(html.unescape(m.group(1)))
                current = tr_json.get("current") or {}
                tid = current.get("id") or tr_json.get("id")
                bid = current.get("band_id") or tr_json.get("band_id")
                if tid:
                    parsed = fetch_release_date_via_api(tid, band_id=bid)
                    if parsed:
                        return parsed
            except Exception:
                pass
    else:
        if DEBUG_HTML:
            _dump_html(url, "<EMPTY>")

    return None


def is_release_date_valid(release_date):
    if release_date is None:
        print("   ⚠️ Дата релиза не найдена на странице, пропуск.")
        return False

    today = datetime.now(timezone.utc).date()
    days_diff = (today - release_date).days

    if days_diff < 0:
        is_ok = ALLOW_UPCOMING
        status = "Анонс / Предзаказ" if is_ok else "Пропущен (предзаказ)"
        print(f"   📅 Дата релиза: {release_date} ({status})")
        return is_ok
    else:
        is_ok = days_diff <= MAX_DAYS_AGO
        status = f"{days_diff} дн. назад" if is_ok else f"Устарел ({days_diff} дн. назад)"
        print(f"   📅 Дата релиза: {release_date} ({status})")
        return is_ok


# ==============================================================================
# ПАРСИНГ ЖАНРОВ / ТЕГОВ
# ==============================================================================
def fetch_from_genre(genre):
    genre_clean = genre.strip().lower().replace(" ", "-")
    if not genre_clean:
        return []

    print(f" 🏷️ [ЖАНР]: Ищем релизы по жанру/тегу '{genre_clean}'...")
    raw_items = []
    seen_links = set()

    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"https://bandcamp.com/tag/{genre_clean}",
        "Origin": "https://bandcamp.com",
        "X-Requested-With": "XMLHttpRequest"
    }

    discover_urls = [
        f"https://bandcamp.com/api/discover/3/get_cards?g={genre_clean}&s=new&p=0&f=all",
        f"https://bandcamp.com/api/discover/3/get_cards?g={genre_clean}&s=top&p=0&f=all"
    ]

    for disc_url in discover_urls:
        if raw_items:
            break

        res_text = ""
        if CURL_CFFI_AVAILABLE:
            try:
                res = curl_requests.get(disc_url, headers=headers, impersonate="chrome120", timeout=20)
                if res.status_code == 200 and is_valid_json_response(res.text):
                    res_text = res.text
            except Exception:
                pass

        if not res_text:
            try:
                res = requests.get(disc_url, headers=headers, timeout=20)
                if res.status_code == 200 and is_valid_json_response(res.text):
                    res_text = res.text
            except Exception:
                pass

        if res_text:
            try:
                data = json.loads(res_text)
                cards = data.get("cards") or data.get("items") or []
                for card in cards:
                    link = card.get("tralbum_url") or card.get("page_url") or card.get("link") or card.get("url")
                    title = card.get("primary_text") or card.get("title") or card.get("album_title")
                    artist = card.get("secondary_text") or card.get("artist_name") or card.get("artist") or "Неизвестный артист"
                    art_id = card.get("art_id") or card.get("image_id")
                    tralbum_id = card.get("tralbum_id") or card.get("id")
                    band_id = card.get("band_id")

                    if link:
                        full_link = link.split('?')[0]
                        if full_link not in seen_links:
                            seen_links.add(full_link)
                            img_url = f"https://f4.bcbits.com/img/a{art_id}_10.jpg" if art_id else ""
                            raw_items.append({
                                "title_full": f"{title} by {artist}" if title else f"Release by {artist}",
                                "artist": artist,
                                "album_title": title or "Без названия",
                                "image": img_url,
                                "description": f"New release in #{genre_clean}.",
                                "tags": [genre_clean],
                                "link": full_link,
                                "genre": genre_clean,
                                "tralbum_id": tralbum_id,
                                "band_id": band_id,
                                "band_name": None,
                            })
            except Exception:
                pass

    if not raw_items:
        api_url = "https://bandcamp.com/api/hub/2/dig_deeper"
        payload = {
            "filters": {"format": "all", "location": 0, "sort": "date", "tag_slug": genre_clean},
            "page": 1
        }
        post_headers = headers.copy()
        post_headers["Content-Type"] = "application/json"

        res_text = ""
        if CURL_CFFI_AVAILABLE:
            try:
                res = curl_requests.post(api_url, json=payload, headers=post_headers, impersonate="chrome120", timeout=20)
                if res.status_code == 200 and is_valid_json_response(res.text):
                    res_text = res.text
            except Exception:
                pass
        if not res_text:
            try:
                res = requests.post(api_url, json=payload, headers=post_headers, timeout=20)
                if res.status_code == 200 and is_valid_json_response(res.text):
                    res_text = res.text
            except Exception:
                pass

        if res_text:
            try:
                data = json.loads(res_text)
                for item in data.get("items", []):
                    link = item.get("tralbum_url") or item.get("link") or item.get("url")
                    title = item.get("title")
                    artist = item.get("artist_name") or item.get("artist") or "Неизвестный артист"
                    art_id = item.get("art_id")
                    tralbum_id = item.get("tralbum_id") or item.get("id")
                    band_id = item.get("band_id")

                    if link:
                        full_link = link.split('?')[0]
                        if full_link not in seen_links:
                            seen_links.add(full_link)
                            img_url = f"https://f4.bcbits.com/img/a{art_id}_10.jpg" if art_id else ""
                            raw_items.append({
                                "title_full": f"{title} by {artist}" if title else f"Release by {artist}",
                                "artist": artist,
                                "album_title": title or "Без названия",
                                "image": img_url,
                                "description": f"New release in #{genre_clean}.",
                                "tags": [genre_clean],
                                "link": full_link,
                                "genre": genre_clean,
                                "tralbum_id": tralbum_id,
                                "band_id": band_id,
                                "band_name": None,
                            })
            except Exception:
                pass

    if not raw_items:
        print(f"   ℹ️ Переход к резервному парсингу страницы https://bandcamp.com/tag/{genre_clean}...")
        tag_url = f"https://bandcamp.com/tag/{genre_clean}?sort_field=date"
        html_text = fetch_html(tag_url)
        if html_text:
            blob_match = re.search(r'data-blob="([^"]+)"', html_text)
            if blob_match:
                try:
                    blob_json = json.loads(html.unescape(blob_match.group(1)))
                    hub_items = (blob_json.get("hub_data", {}).get("dig_deeper", {}).get("items", []) or
                                 blob_json.get("tab_data", {}).get("dig_deeper", {}).get("items", []) or
                                 blob_json.get("dig_deeper", {}).get("items", []) or
                                 blob_json.get("items", []))
                    for item in hub_items:
                        link = item.get("tralbum_url") or item.get("link") or item.get("page_url")
                        title = item.get("title") or item.get("primary_text")
                        artist = item.get("artist_name") or item.get("artist") or item.get("secondary_text") or "Неизвестный артист"
                        art_id = item.get("art_id") or item.get("image_id")
                        tralbum_id = item.get("tralbum_id") or item.get("id")
                        band_id = item.get("band_id")
                        if link:
                            full_link = link.split('?')[0]
                            if full_link not in seen_links:
                                seen_links.add(full_link)
                                img_url = f"https://f4.bcbits.com/img/a{art_id}_10.jpg" if art_id else ""
                                raw_items.append({
                                    "title_full": f"{title} by {artist}" if title else f"Release by {artist}",
                                    "artist": artist,
                                    "album_title": title or "Без названия",
                                    "image": img_url,
                                    "description": f"New release in #{genre_clean}.",
                                    "tags": [genre_clean],
                                    "link": full_link,
                                    "genre": genre_clean,
                                    "tralbum_id": tralbum_id,
                                    "band_id": band_id,
                                    "band_name": None,
                                })
                except Exception as e:
                    print(f"   ⚠️ Ошибка извлечения data-blob: {e}")

    print(f"   Найдено релизов по жанру '{genre_clean}': {len(raw_items)}")
    return raw_items


# ==============================================================================
# ПАРСИНГ АРТИСТОВ / ЛЕЙБЛОВ
# ==============================================================================
def extract_subdomain(target):
    target = target.strip().lower()
    target = re.sub(r'^https?://', '', target)
    target = target.split('.')[0]
    target = target.split('/')[0]
    return target


def _extract_band_id_from_artist_page(html_text):
    """Пытается достать band_id из bc-page-properties или из data-* атрибутов."""
    props = re.search(
        r'<meta\s+name=["\']bc-page-properties["\']\s+content=["\']([^"\']+)["\']',
        html_text, re.I
    )
    if props:
        try:
            pj = json.loads(html.unescape(props.group(1)))
            if isinstance(pj, dict):
                # Разные варианты ключа
                for k in ("band_id", "item_id", "id"):
                    if pj.get(k):
                        return pj[k]
        except Exception:
            pass
    m = re.search(r'band_id["\']?\s*[:=]\s*(\d+)', html_text)
    if m:
        return int(m.group(1))
    return None


def fetch_from_artist_or_label(target):
    subdomain = extract_subdomain(target)
    if not subdomain:
        return []

    url = f"https://{subdomain}.bandcamp.com/music"
    print(f" 👤 [АРТИСТ/ЛЕЙБЛ]: Проверяем каталог {subdomain} ({url})...")

    html_text = fetch_html(url)
    if not html_text:
        print(f"   ⚠️ Не удалось получить страницу артиста {subdomain}")
        return []

    artist_match = re.search(r'<meta\s+property="og:site_name"\s+content="([^"]+)"', html_text, re.IGNORECASE)
    if not artist_match:
        artist_match = re.search(r'<title>([^<]+)</title>', html_text, re.IGNORECASE)
    artist_name = artist_match.group(1).split('|')[0].strip() if artist_match else subdomain

    band_id = _extract_band_id_from_artist_page(html_text)

    releases_map = {}

    client_items_match = re.search(r'data-client-items="([^"]+)"', html_text)
    if client_items_match:
        try:
            raw_json = html.unescape(client_items_match.group(1))
            client_items = json.loads(raw_json)
            for c_item in client_items:
                path = c_item.get("page_url") or c_item.get("title_link")
                title = c_item.get("title")
                art_id = c_item.get("art_id")
                tralbum_id = c_item.get("id") or c_item.get("tralbum_id")
                item_type = c_item.get("item_type") or ("track" if "/track/" in (path or "") else "album")

                if path and title:
                    full_link = f"https://{subdomain}.bandcamp.com{path}" if path.startswith("/") else path
                    full_link = full_link.split('?')[0].replace(".bandcamp.com/a/", ".bandcamp.com/album/").replace(".bandcamp.com/t/", ".bandcamp.com/track/")
                    if "/album/" in full_link or "/track/" in full_link:
                        img_url = f"https://f4.bcbits.com/img/a{art_id}_10.jpg" if art_id else ""
                        releases_map[full_link] = {
                            "title_full": f"{title} by {artist_name}",
                            "artist": artist_name,
                            "album_title": title,
                            "image": img_url,
                            "description": f"New release from {artist_name}.",
                            "tags": [subdomain],
                            "link": full_link,
                            "genre": "artist/label",
                            "tralbum_id": tralbum_id,
                            "band_id": band_id,
                            "band_name": subdomain,
                        }
        except Exception as e:
            print(f"   ⚠️ Ошибка чтения data-client-items: {e}")

    # Дополнительные пути без ID (попадут в fallback через HTML)
    all_paths = re.findall(r'(?:href=["\']|\\?/)(/(?:album|track)/[a-zA-Z0-9\-_]+)', html_text, re.IGNORECASE)
    for path in all_paths:
        full_link = f"https://{subdomain}.bandcamp.com{path}".split('?')[0]
        if full_link not in releases_map:
            slug = path.split("/")[-1]
            title_slug = slug.replace("-", " ").title()
            releases_map[full_link] = {
                "title_full": f"{title_slug} by {artist_name}",
                "artist": artist_name,
                "album_title": title_slug,
                "image": "",
                "description": f"New release from {artist_name}.",
                "tags": [subdomain],
                "link": full_link,
                "genre": "artist/label",
                "tralbum_id": None,
                "band_id": band_id,
                "band_name": subdomain,
            }

    js_paths = re.findall(r'"(?:page_url|title_link)"\s*:\s*"([^"]+)"', html_text)
    for path in js_paths:
        clean_path = path.replace("\\/", "/")
        if clean_path.startswith("/") and ("/album/" in clean_path or "/track/" in clean_path):
            full_link = f"https://{subdomain}.bandcamp.com{clean_path}".split('?')[0]
            if full_link not in releases_map:
                slug = clean_path.split("/")[-1]
                title_slug = slug.replace("-", " ").title()
                releases_map[full_link] = {
                    "title_full": f"{title_slug} by {artist_name}",
                    "artist": artist_name,
                    "album_title": title_slug,
                    "image": "",
                    "description": f"New release from {artist_name}.",
                    "tags": [subdomain],
                    "link": full_link,
                    "genre": "artist/label",
                    "tralbum_id": None,
                    "band_id": band_id,
                    "band_name": subdomain,
                }

    raw_items = list(releases_map.values())
    with_id = sum(1 for r in raw_items if r.get("tralbum_id"))
    print(f"   Найдено релизов в каталоге {artist_name}: {len(raw_items)} (с tralbum_id: {with_id})")
    return raw_items


# ==============================================================================
# TELEGRAM
# ==============================================================================
def send_to_telegram(release):
    if not TELEGRAM_BOT_TOKEN:
        print(" Ошибка: TELEGRAM_BOT_TOKEN не задан")
        return False

    title = html.escape(release["title_full"])
    desc = html.escape(release["description"])

    tags_list = [f"#{t.lower().replace('-', '_').replace(' ', '_')}" for t in release["tags"]]
    main_hashtag = f"#{release['genre'].lower().replace('-', '_').replace(' ', '_')}"

    if main_hashtag not in tags_list and release['genre'] != "artist/label":
        tags_list.insert(0, main_hashtag)
    tags_str = " ".join(tags_list)

    release_date = release.get("release_date")
    date_line = ""
    if release_date:
        date_line = f"📅 Дата релиза: <b>{release_date.strftime('%d %b %Y')}</b>\n\n"

    caption = (
        f"🌌 <b>{title}</b>\n\n"
        f"{date_line}"
        f"📝 <i>{desc}</i>\n\n"
        f"🏷️ {tags_str}\n\n"
        f"🔗 <a href=\"{release['link']}\">Слушать / Купить на Bandcamp</a>"
    )

    if release.get("image"):
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "photo": release["image"],
            "caption": caption,
            "parse_mode": "HTML"
        }
    else:
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": caption,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        }

    try:
        resp = requests.post(api_url, data=payload, timeout=20)
        return resp.ok
    except Exception as e:
        print(f"❌ Ошибка отправки в Telegram: {e}")
        return False


# ==============================================================================
# MAIN
# ==============================================================================
def main():
    if not TELEGRAM_BOT_TOKEN:
        print("Ошибка: Не задан TELEGRAM_BOT_TOKEN")
        return

    if CURL_CFFI_AVAILABLE:
        print("✅ curl_cffi доступен — используем имперсонацию Chrome")
    else:
        print("⚠️ curl_cffi НЕ установлен — рекомендуется: pip install curl-cffi")
        print("   Без него Bandcamp будет часто отдавать челлендж Cloudflare.")

    init_csv_file()
    posted = load_posted()

    releases = []
    seen_links = set()

    for target in TARGET_ARTISTS_AND_LABELS:
        if target.strip():
            artist_items = fetch_from_artist_or_label(target)
            for details in artist_items:
                if details and details["link"] and details["link"] not in seen_links:
                    seen_links.add(details["link"])
                    releases.append(details)

    for genre in GENRES:
        if genre.strip():
            genre_items = fetch_from_genre(genre)
            for details in genre_items:
                if details and details["link"] and details["link"] not in seen_links:
                    seen_links.add(details["link"])
                    releases.append(details)

    print(f"\n🔎 Всего распознано уникальных релизов: {len(releases)}")

    new_releases = [r for r in releases if r["link"] not in posted]
    print(f"✨ Не опубликованных ранее релизов: {len(new_releases)}")

    if not new_releases:
        print("🏁 Нет новых релизов для обработки.")
        return

    print(f"\n📅 Извлекаем фактические даты релизов для {len(new_releases)} релизов...")
    for i, release in enumerate(new_releases, 1):
        print(f"   🔎 [{i}/{len(new_releases)}] {release['title_full']}...")
        release["release_date"] = fetch_release_date(release)
        if release["release_date"]:
            print(f"      ✅ {release['release_date']}")
        else:
            print(f"      ❌ Не найдена")
        # Случайная пауза, чтобы не выглядеть как бот
        time.sleep(random.uniform(2.0, 4.0))

    new_releases.sort(
        key=lambda x: x["release_date"] if x["release_date"] is not None else date.min,
        reverse=True
    )

    print("\n📊 Итоговый порядок релизов, отсортированный по дате выхода:")
    for r in new_releases:
        d_str = r['release_date'].strftime('%Y-%m-%d') if r['release_date'] else 'Дата неизвестна'
        print(f"   • {d_str} — {r['title_full']}")

    new_posts = 0
    for release in new_releases:
        if new_posts >= MAX_POSTS_PER_RUN:
            print(f"\n🛑 Достигнут лимит в {MAX_POSTS_PER_RUN} постов за запуск. Остановка.")
            break

        print(f"\n🔍 Оценка публикации: {release['title_full']}")
        rel_date = release.get("release_date")

        if not is_release_date_valid(rel_date):
            continue

        print(f"🚀 Публикация в Telegram: {release['title_full']}")
        if send_to_telegram(release):
            posted.add(release["link"])
            save_to_csv(release)
            new_posts += 1
            time.sleep(3)
        else:
            print(f"❌ Не удалось отправить в Telegram: {release['link']}")

    save_posted(posted)
    print(f"\n🏁 Завершено. Опубликовано новых релизов: {new_posts}")


if __name__ == "__main__":
    main()
