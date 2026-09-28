import os
import json
import html
import time
import csv
import re
import random
import threading
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

DEBUG_HTML = True
DEBUG_HTML_DIR = "debug_html"
FETCH_RETRIES = 3
COOLDOWN_AFTER_FAIL = 15           # сек, если после всех попыток всё равно челлендж
DELAY_BETWEEN_RELEASES = (5.0, 8.0)  # мин/макс пауза между страницами релизов

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@bc_ambient")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

# ==============================================================================
# ПОСТОЯННАЯ СЕССИЯ curl_cffi (главное отличие от предыдущей версии)
# ==============================================================================
_session_lock = threading.Lock()
_curl_session = None


def get_curl_session():
    """Единая сессия curl_cffi с impersonate Chrome.

    Переиспользуется для всех запросов, чтобы сохранять Cloudflare-cookie
    (__cf_bm и т.п.) между запросами. Это критично для обхода челленджа.
    """
    global _curl_session
    if not CURL_CFFI_AVAILABLE:
        return None
    if _curl_session is None:
        with _session_lock:
            if _curl_session is None:
                _curl_session = curl_requests.Session(impersonate="chrome120")
    return _curl_session


def get_headers(accept="text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"):
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
        "Upgrade-Insecure-Requests": "1",
    }


# ==============================================================================
# ВАЛИДАЦИЯ ОТВЕТОВ
# ==============================================================================
def is_valid_html_response(text):
    """Возвращает True только если HTML похож на настоящую страницу Bandcamp.

    Челлендж Cloudflare (< 5000 байт, нет маркеров Bandcamp) считается
    НЕвалидным — иначе парсер молча возвращает None и мы теряем диагностику.
    """
    if not text or len(text) < 500:
        return False
    low = text.lower()
    anti_bot = (
        "just a moment", "enable javascript", "attention required",
        "<title>access denied</title>", "cf-browser-verification",
        "checking your browser", "please verify you are a human",
        "ddos protection", "ray id", "cf_chl_",
    )
    if any(x in low for x in anti_bot):
        return False
    # Эвристика: короткие ответы (< 5000 байт) без маркеров Bandcamp — челлендж
    bandcamp_markers = ("data-tralbum", "bc-page-properties", "bcbits.com",
                        "tralbum-credits", "bandcamp.com/")
    has_marker = any(m in low for m in bandcamp_markers)
    if len(text) < 5000 and not has_marker:
        return False
    if not has_marker:
        return False
    return True


def is_cf_challenge(text):
    """Быстрая проверка: похоже ли на страницу-челлендж Cloudflare."""
    if not text:
        return False
    low = text.lower()
    hints = ("cf_chl_", "challenge-platform", "cf-browser-verification",
             "checking your browser", "just a moment", "enable javascript")
    return any(h in low for h in hints)


def is_valid_json_response(text):
    if not text or len(text) < 10:
        return False
    try:
        json.loads(text)
        return True
    except Exception:
        return False


def _dump_html(url, text, suffix=""):
    if not DEBUG_HTML:
        return
    try:
        os.makedirs(DEBUG_HTML_DIR, exist_ok=True)
        safe = re.sub(r'[^a-zA-Z0-9]+', '_', url)[-120:]
        path = os.path.join(DEBUG_HTML_DIR, f"{safe}{suffix}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text or "")
    except Exception:
        pass


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
# ПАРСИНГ ДАТЫ
# ==============================================================================
def _strip_tags(text):
    if not text:
        return ""
    text = re.sub(r'<script[^>]*>.*?</script>', ' ', text, flags=re.I | re.S)
    text = re.sub(r'<style[^>]*>.*?</style>', ' ', text, flags=re.I | re.S)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html.unescape(text)
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

    tralbum_match = re.search(r'data-tralbum=["\']([^"\']+)["\']', html_text)
    if tralbum_match:
        try:
            tr_json = json.loads(html.unescape(tralbum_match.group(1)))
            parsed = _find_date_in_dict(tr_json)
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

    props = re.search(
        r'<meta\s+name=["\']bc-page-properties["\']\s+content=["\']([^"\']+)["\']',
        html_text, re.I
    )
    if props:
        try:
            pj = json.loads(html.unescape(props.group(1)))
            parsed = _find_date_in_dict(pj)
            if parsed:
                return parsed
        except Exception:
            pass

    credits = re.search(
        r'class=["\'][^"\']*tralbum-credits[^"\']*["\'][^>]*>(.*?)</div>',
        html_text, re.I | re.S
    )
    if credits:
        bt = _strip_tags(credits.group(1))
        m = re.search(
            r'released?\s+(?:on\s+)?([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})',
            bt, re.I
        )
        if m:
            parsed = _try_parse_date_string(m.group(1))
            if parsed:
                return parsed

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
        r'"?\s*[:=]\s*"([^"]+)"', html_text, re.I
    ):
        parsed = _try_parse_date_string(raw)
        if parsed:
            return parsed

    return None


# ==============================================================================
# МОБИЛЬНОЕ API BANDCAMP
# ==============================================================================
API_ENDPOINTS = [
    "https://bandcamp.com/api/mobile/25/tralbum_details",
    "https://bandcamp.com/api/mobile/24/tralbum_details",
    "https://bandcamp.com/api/mobile/23/tralbum_details",
    "https://bandcamp.com/api/tralbum/2/tralbum_details",
    "https://bandcamp.com/api/tralbum/1/tralbum_details",
]


def fetch_release_date_via_api(tralbum_id, band_id=None, band_name=None, tralbum_type="a"):
    """Пробуем все известные версии мобильного API."""
    if not tralbum_id:
        return None

    params_variants = []
    if band_id:
        params_variants.append({"band_id": band_id, "tralbum_id": tralbum_id, "tralbum_type": tralbum_type})
    if band_name:
        params_variants.append({"band_name": band_name, "tralbum_id": tralbum_id, "tralbum_type": tralbum_type})
    params_variants.append({"tralbum_id": tralbum_id, "tralbum_type": tralbum_type})

    session = get_curl_session()

    for endpoint in API_ENDPOINTS:
        for params in params_variants:
            try:
                url = endpoint + "?" + urllib.parse.urlencode(params)
                if session is not None:
                    r = session.get(url, headers=get_headers("application/json, */*"), timeout=15)
                else:
                    r = requests.get(url, headers=get_headers("application/json, */*"), timeout=15)

                if r.status_code != 200:
                    continue
                if not is_valid_json_response(r.text):
                    continue
                data = r.json()
                parsed = _find_date_in_dict(data)
                if parsed:
                    return parsed
            except Exception:
                continue
    return None


# ==============================================================================
# ЗАГРУЗКА HTML С RETRY + COOLDOWN
# ==============================================================================
def _one_attempt(url, headers, timeout):
    """Одна попытка: curl_cffi Session → requests. Возвращает (ok, text, source)."""
    session = get_curl_session()
    if session is not None:
        try:
            res = session.get(url, headers=headers, timeout=timeout)
            if res.status_code == 200:
                return (is_valid_html_response(res.text), res.text, "curl_cffi")
            return (False, res.text or "", f"curl_cffi_http_{res.status_code}")
        except Exception as e:
            return (False, "", f"curl_cffi_err:{e}")
    # fallback на requests
    try:
        res = requests.get(url, headers=headers, timeout=timeout)
        if res.status_code == 200:
            return (is_valid_html_response(res.text), res.text, "requests")
        return (False, res.text or "", f"requests_http_{res.status_code}")
    except Exception as e:
        return (False, "", f"requests_err:{e}")


def fetch_html(url, referer=None, timeout=30, retries=FETCH_RETRIES, dump_cf=True):
    headers = get_headers()
    if referer:
        headers["Referer"] = referer

    last_status = None
    last_len = 0
    last_cf_text = ""

    for attempt in range(1, retries + 1):
        ok, text, source = _one_attempt(url, headers, timeout)
        last_status = source
        last_len = len(text or "")

        if ok:
            if attempt > 1:
                print(f"   ✓ OK на попытке {attempt} ({source})")
            return text

        # Челлендж Cloudflare — сохраняем для диагностики
        if is_cf_challenge(text):
            last_cf_text = text
            if dump_cf:
                _dump_html(url, text, suffix="_cf")

        if attempt < retries:
            pause = 2 ** attempt + random.uniform(0, 1)
            print(f"   ↻ Попытка {attempt}: {source}, len={last_len}. Пауза {pause:.1f}с")
            time.sleep(pause)

    # Финальный cooldown + ещё одна попытка
    if last_cf_text:
        print(f"   ❄️ Cloudflare-челлендж. Ждём {COOLDOWN_AFTER_FAIL}с и пробуем ещё раз...")
        time.sleep(COOLDOWN_AFTER_FAIL)
        ok, text, source = _one_attempt(url, headers, timeout)
        if ok:
            print(f"   ✓ OK после cooldown ({source})")
            return text

    print(f"   ❌ HTML не получен (last={last_status}, len={last_len})")
    return ""


def fetch_release_date(release):
    url = release.get("link")
    tralbum_id = release.get("tralbum_id")
    band_id = release.get("band_id")
    band_name = release.get("band_name")

    # 1. Мобильное API (не требует HTML — не пачкает сессию)
    if tralbum_id:
        parsed = fetch_release_date_via_api(tralbum_id, band_id=band_id, band_name=band_name)
        if parsed:
            print(f"      ✅ (API) {parsed}")
            return parsed

    # 2. HTML страницы релиза
    html_text = fetch_html(url)
    if html_text:
        parsed = parse_date_from_html(html_text)
        if parsed:
            print(f"      ✅ (HTML) {parsed}")
            return parsed

        # Fallback: нашли ID в data-tralbum — пробуем API
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
                        print(f"      ✅ (API-fallback) {parsed}")
                        return parsed
            except Exception:
                pass

    return None


def is_release_date_valid(release_date):
    if release_date is None:
        print("   ⚠️ Дата релиза не найдена на странице, пропуск.")
        return False
    today = datetime.now(timezone.utc).date()
    days_diff = (today - release_date).days
    if days_diff < 0:
        is_ok = ALLOW_UPCOMING
        print(f"   📅 Дата релиза: {release_date} ({'Анонс / Предзаказ' if is_ok else 'Пропущен (предзаказ)'})")
        return is_ok
    is_ok = days_diff <= MAX_DAYS_AGO
    print(f"   📅 Дата релиза: {release_date} ({f'{days_diff} дн. назад' if is_ok else f'Устарел ({days_diff} дн. назад)'})")
    return is_ok


# ==============================================================================
# ПАРСИНГ ЖАНРОВ
# ==============================================================================
def fetch_from_genre(genre):
    genre_clean = genre.strip().lower().replace(" ", "-")
    if not genre_clean:
        return []

    print(f" 🏷️ [ЖАНР]: '{genre_clean}'...")
    raw_items = []
    seen_links = set()
    session = get_curl_session()

    api_headers = get_headers("application/json, */*")

    discover_urls = [
        f"https://bandcamp.com/api/discover/3/get_cards?g={genre_clean}&s=new&p=0&f=all",
        f"https://bandcamp.com/api/discover/3/get_cards?g={genre_clean}&s=top&p=0&f=all",
    ]
    for disc_url in discover_urls:
        if raw_items:
            break
        res_text = ""
        try:
            if session:
                r = session.get(disc_url, headers=api_headers, timeout=20)
            else:
                r = requests.get(disc_url, headers=api_headers, timeout=20)
            if r.status_code == 200 and is_valid_json_response(r.text):
                res_text = r.text
        except Exception:
            pass
        if res_text:
            try:
                data = json.loads(res_text)
                for card in data.get("cards") or data.get("items") or []:
                    link = card.get("tralbum_url") or card.get("page_url") or card.get("link") or card.get("url")
                    title = card.get("primary_text") or card.get("title") or card.get("album_title")
                    artist = card.get("secondary_text") or card.get("artist_name") or card.get("artist") or "Неизвестный артист"
                    art_id = card.get("art_id") or card.get("image_id")
                    tid = card.get("tralbum_id") or card.get("id")
                    bid = card.get("band_id")
                    if link:
                        fl = link.split('?')[0]
                        if fl not in seen_links:
                            seen_links.add(fl)
                            raw_items.append({
                                "title_full": f"{title} by {artist}" if title else f"Release by {artist}",
                                "artist": artist,
                                "album_title": title or "Без названия",
                                "image": f"https://f4.bcbits.com/img/a{art_id}_10.jpg" if art_id else "",
                                "description": f"New release in #{genre_clean}.",
                                "tags": [genre_clean],
                                "link": fl,
                                "genre": genre_clean,
                                "tralbum_id": tid,
                                "band_id": bid,
                                "band_name": None,
                            })
            except Exception:
                pass

    print(f"   Найдено по '{genre_clean}': {len(raw_items)}")
    return raw_items


# ==============================================================================
# ПАРСИНГ АРТИСТОВ / ЛЕЙБЛОВ
# ==============================================================================
def extract_subdomain(target):
    target = target.strip().lower()
    target = re.sub(r'^https?://', '', target)
    return target.split('.')[0].split('/')[0]


def _extract_band_id_from_artist_page(html_text):
    props = re.search(
        r'<meta\s+name=["\']bc-page-properties["\']\s+content=["\']([^"\']+)["\']',
        html_text, re.I
    )
    if props:
        try:
            pj = json.loads(html.unescape(props.group(1)))
            if isinstance(pj, dict):
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
    print(f" 👤 [АРТИСТ/ЛЕЙБЛ]: {subdomain} ({url})...")

    html_text = fetch_html(url)
    if not html_text:
        print(f"   ⚠️ Не удалось получить страницу артиста {subdomain}")
        return []

    am = re.search(r'<meta\s+property="og:site_name"\s+content="([^"]+)"', html_text, re.I) \
         or re.search(r'<title>([^<]+)</title>', html_text, re.I)
    artist_name = am.group(1).split('|')[0].strip() if am else subdomain

    band_id = _extract_band_id_from_artist_page(html_text)

    releases_map = {}

    client_items_match = re.search(r'data-client-items="([^"]+)"', html_text)
    if client_items_match:
        try:
            client_items = json.loads(html.unescape(client_items_match.group(1)))

            # Диагностика: что лежит в первом элементе
            if client_items:
                sample = client_items[0]
                print(f"   🔬 data-client-items[0] ключи: {list(sample.keys())}")
                if "release_date" in sample:
                    print(f"      → обнаружен release_date: {sample['release_date']}")

            for c_item in client_items:
                path = c_item.get("page_url") or c_item.get("title_link")
                title = c_item.get("title")
                art_id = c_item.get("art_id")
                tid = c_item.get("id") or c_item.get("tralbum_id") or c_item.get("item_id")
                # Пробуем взять дату прямо здесь, если Bandcamp её отдаёт
                inline_date = None
                for k in ("release_date", "album_release_date", "date"):
                    if c_item.get(k):
                        inline_date = _try_parse_date_string(str(c_item[k]))
                        if inline_date:
                            break

                if path and title:
                    fl = f"https://{subdomain}.bandcamp.com{path}" if path.startswith("/") else path
                    fl = fl.split('?')[0].replace(".bandcamp.com/a/", ".bandcamp.com/album/").replace(".bandcamp.com/t/", ".bandcamp.com/track/")
                    if "/album/" in fl or "/track/" in fl:
                        releases_map[fl] = {
                            "title_full": f"{title} by {artist_name}",
                            "artist": artist_name,
                            "album_title": title,
                            "image": f"https://f4.bcbits.com/img/a{art_id}_10.jpg" if art_id else "",
                            "description": f"New release from {artist_name}.",
                            "tags": [subdomain],
                            "link": fl,
                            "genre": "artist/label",
                            "tralbum_id": tid,
                            "band_id": band_id,
                            "band_name": subdomain,
                            "release_date": inline_date,
                        }
        except Exception as e:
            print(f"   ⚠️ data-client-items: {e}")

    # Добираем пути без ID
    for path in re.findall(r'(?:href=["\']|\\?/)(/(?:album|track)/[a-zA-Z0-9\-_]+)', html_text, re.I):
        fl = f"https://{subdomain}.bandcamp.com{path}".split('?')[0]
        if fl not in releases_map:
            slug = path.split("/")[-1]
            t = slug.replace("-", " ").title()
            releases_map[fl] = {
                "title_full": f"{t} by {artist_name}",
                "artist": artist_name,
                "album_title": t,
                "image": "",
                "description": f"New release from {artist_name}.",
                "tags": [subdomain],
                "link": fl,
                "genre": "artist/label",
                "tralbum_id": None,
                "band_id": band_id,
                "band_name": subdomain,
                "release_date": None,
            }

    raw_items = list(releases_map.values())
    with_id = sum(1 for r in raw_items if r.get("tralbum_id"))
    with_date = sum(1 for r in raw_items if r.get("release_date"))
    print(f"   Найдено: {len(raw_items)} (с tralbum_id: {with_id}, с датой из data-client-items: {with_date})")
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
        return requests.post(api_url, data=payload, timeout=20).ok
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

    if CURL_CFFI_AVAILABLE:
        print("✅ curl_cffi доступен — используется единая Session с impersonate Chrome")
    else:
        print("⚠️ curl_cffi НЕ установлен — Bandcamp будет часто отдавать челлендж. "
              "Рекомендуется: pip install curl-cffi")

    init_csv_file()
    posted = load_posted()

    releases = []
    seen_links = set()

    for target in TARGET_ARTISTS_AND_LABELS:
        if target.strip():
            for details in fetch_from_artist_or_label(target):
                if details and details["link"] and details["link"] not in seen_links:
                    seen_links.add(details["link"])
                    releases.append(details)

    for genre in GENRES:
        if genre.strip():
            for details in fetch_from_genre(genre):
                if details and details["link"] and details["link"] not in seen_links:
                    seen_links.add(details["link"])
                    releases.append(details)

    print(f"\n🔎 Всего: {len(releases)}")
    new_releases = [r for r in releases if r["link"] not in posted]
    print(f"✨ Новых: {len(new_releases)}")

    if not new_releases:
        print("🏁 Нет новых релизов.")
        return

    print(f"\n📅 Извлекаем даты для {len(new_releases)} релизов...")
    for i, release in enumerate(new_releases, 1):
        # Если дата уже есть из data-client-items — не ходим на страницу вообще
        if release.get("release_date"):
            print(f"   [{i}/{len(new_releases)}] {release['title_full']}")
            print(f"      ✅ (из каталога) {release['release_date']}")
            continue

        print(f"   [{i}/{len(new_releases)}] {release['title_full']}...")
        release["release_date"] = fetch_release_date(release)
        if not release["release_date"]:
            print(f"      ❌ Не найдена")

        time.sleep(random.uniform(*DELAY_BETWEEN_RELEASES))

    new_releases.sort(
        key=lambda x: x["release_date"] if x["release_date"] is not None else date.min,
        reverse=True
    )

    print("\n📊 Итог:")
    for r in new_releases:
        d = r['release_date'].strftime('%Y-%m-%d') if r['release_date'] else 'Дата неизвестна'
        print(f"   • {d} — {r['title_full']}")

    new_posts = 0
    for release in new_releases:
        if new_posts >= MAX_POSTS_PER_RUN:
            print(f"\n🛑 Лимит {MAX_POSTS_PER_RUN} постов.")
            break
        print(f"\n🔍 {release['title_full']}")
        if not is_release_date_valid(release.get("release_date")):
            continue
        print(f"🚀 Публикуем: {release['title_full']}")
        if send_to_telegram(release):
            posted.add(release["link"])
            save_to_csv(release)
            new_posts += 1
            time.sleep(3)
        else:
            print(f"❌ Telegram fail: {release['link']}")

    save_posted(posted)
    print(f"\n🏁 Опубликовано: {new_posts}")


if __name__ == "__main__":
    main()
