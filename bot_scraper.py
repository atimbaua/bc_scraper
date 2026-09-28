import os
import json
import html
import time
import csv
import re
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

MAX_DAYS_AGO = 30        # Публиковать релизы не старше N дней
ALLOW_UPCOMING = True    # Публиковать предзаказы / анонсы

MAX_POSTS_PER_RUN = 5
POSTED_FILE = "posted_releases.json"
CSV_FILE = "releases_data.csv"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@bc_ambient")


def get_headers():
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }


def is_valid_html_response(text):
    if not text or len(text) < 100:
        return False
    low = text.lower()
    if "just a moment" in low or "enable javascript" in low or "attention required" in low or "<title>access denied</title>" in low:
        return False
    return True


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
            published_at_utc,
            release_date_str,
            genre,
            artist,
            album_title,
            url,
            tags,
            image_url
        ])


# ==============================================================================
# ПАРСИНГ ДАТЫ СО СТРАНИЦЫ РЕЛИЗА
# ==============================================================================
def _strip_tags(text):
    """Удаляет HTML-теги и нормализует пробелы. Полезно для regex-поиска по тексту."""
    if not text:
        return ""
    text = re.sub(r'<script[^>]*>.*?</script>', ' ', text, flags=re.I | re.S)
    text = re.sub(r'<style[^>]*>.*?</style>', ' ', text, flags=re.I | re.S)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html.unescape(text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _try_parse_date_string(d_str):
    """Пытается распарсить дату из строки. Поддерживает множество форматов."""
    if not d_str:
        return None

    s = str(d_str).strip()
    if not s:
        return None

    # Убираем время и таймзону в разных вариантах:
    #   '2026-09-26T00:00:00Z'  ->  '2026-09-26'
    #   '26 Sep 2026 00:00:00 GMT' -> '26 Sep 2026'
    #   '2026-09-26T00:00:00+00:00' -> '2026-09-26'
    s = re.sub(
        r'[T\s]\d{1,2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+\-]\d{2}:?\d{2})?.*$',
        '',
        s
    ).strip()
    s = s.rstrip('Z').strip()
    s = s.rstrip(',').strip()

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

    # Fallback: ищем подстроку с датой внутри произвольного текста
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
    """Рекурсивно ищет дату в словаре/списке по заданным ключам."""
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
    """Извлекает дату релиза из HTML страницы Bandcamp.

    Порядок проверок: от самых надёжных источников к самым хрупким.
    """
    if not html_text:
        return None

    # ------------------------------------------------------------------
    # 1. data-tralbum (главный источник, есть практически всегда)
    # ------------------------------------------------------------------
    tralbum_match = re.search(r'data-tralbum=["\']([^"\']+)["\']', html_text)
    if tralbum_match:
        try:
            tr_json = json.loads(html.unescape(tralbum_match.group(1)))
            parsed = _find_date_in_dict(tr_json)
            if parsed:
                return parsed
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 2. JSON-LD (Schema.org MusicAlbum)
    # ------------------------------------------------------------------
    ld_matches = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html_text,
        re.IGNORECASE | re.DOTALL
    )
    for ld_raw in ld_matches:
        try:
            ld_data = json.loads(ld_raw.strip())
            items = ld_data if isinstance(ld_data, list) else [ld_data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                date_str = item.get("datePublished") or item.get("releaseDate")
                if date_str:
                    parsed = _try_parse_date_string(str(date_str))
                    if parsed:
                        return parsed
                # иногда вложено в @graph
                graph = item.get("@graph")
                if isinstance(graph, list):
                    for g in graph:
                        if isinstance(g, dict):
                            ds = g.get("datePublished") or g.get("releaseDate")
                            if ds:
                                parsed = _try_parse_date_string(str(ds))
                                if parsed:
                                    return parsed
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 3. <time datetime="...">
    # ------------------------------------------------------------------
    for dt in re.findall(r'<time[^>]*datetime=["\']([^"\']+)["\']', html_text, re.I):
        parsed = _try_parse_date_string(dt)
        if parsed:
            return parsed

    # ------------------------------------------------------------------
    # 4. <meta name="bc-page-properties"> (JSON)
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # 5. Блок tralbum-credits (по тексту без тегов)
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # 6. og:description и meta[name=description]
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # 7. Общий regex по тексту без тегов ("released ...")
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # 8. itemprop="datePublished"
    # ------------------------------------------------------------------
    meta_dp = re.search(
        r'itemprop=["\']datePublished["\'][^>]*content=["\']([^"\']+)["\']',
        html_text, re.I
    )
    if meta_dp:
        parsed = _try_parse_date_string(meta_dp.group(1))
        if parsed:
            return parsed
    # обратный порядок атрибутов
    meta_dp2 = re.search(
        r'content=["\']([^"\']+)["\'][^>]*itemprop=["\']datePublished["\']',
        html_text, re.I
    )
    if meta_dp2:
        parsed = _try_parse_date_string(meta_dp2.group(1))
        if parsed:
            return parsed

    # ------------------------------------------------------------------
    # 9. JS-переменные (последний резерв)
    # ------------------------------------------------------------------
    for raw_date_str in re.findall(
        r'(?:album_release_date|release_date|current_release_date|original_release_date)'
        r'"?\s*[:=]\s*"([^"]+)"',
        html_text, re.I
    ):
        parsed = _try_parse_date_string(raw_date_str)
        if parsed:
            return parsed

    return None


def _extract_ids_from_tralbum(html_text):
    """Достаёт tralbum_id и band_id из data-tralbum для fallback через API."""
    m = re.search(r'data-tralbum=["\']([^"\']+)["\']', html_text)
    if not m:
        return None, None
    try:
        tr = json.loads(html.unescape(m.group(1)))
    except Exception:
        return None, None

    current = tr.get("current") or {}
    tralbum_id = current.get("id") or tr.get("id")
    band_id = current.get("band_id") or tr.get("band_id")
    return tralbum_id, band_id


def fetch_release_date_via_api(tralbum_id, band_id, tralbum_type="a"):
    """Fallback: мобильное API Bandcamp, если HTML не отдал дату."""
    if not tralbum_id or not band_id:
        return None
    url = (
        f"https://bandcamp.com/api/mobile/24/tralbum_details"
        f"?band_id={band_id}&tralbum_id={tralbum_id}&tralbum_type={tralbum_type}"
    )
    try:
        r = requests.get(url, headers=get_headers(), timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
        parsed = _find_date_in_dict(data)
        if parsed:
            return parsed
    except Exception:
        pass
    return None


def fetch_html(url, referer=None, timeout_html=30, timeout_req=20):
    """Двухступенчатый fetch: curl_cffi → requests. Возвращает HTML или ''."""
    headers = get_headers()
    if referer:
        headers["Referer"] = referer

    html_text = ""

    if CURL_CFFI_AVAILABLE:
        try:
            res = curl_requests.get(
                url, headers=headers, impersonate="chrome120", timeout=timeout_html
            )
            if res.status_code == 200 and is_valid_html_response(res.text):
                html_text = res.text
        except Exception as e:
            print(f"   ⚠️ curl_cffi: {e}")

    if not html_text:
        try:
            res = requests.get(url, headers=headers, timeout=timeout_req)
            if res.status_code == 200 and is_valid_html_response(res.text):
                html_text = res.text
        except Exception as e:
            print(f"   ⚠️ requests: {e}")

    return html_text


def fetch_release_date(url):
    html_text = fetch_html(url)
    if not html_text:
        return None

    parsed = parse_date_from_html(html_text)
    if parsed:
        return parsed

    # Fallback: мобильное API
    tralbum_id, band_id = _extract_ids_from_tralbum(html_text)
    if tralbum_id and band_id:
        parsed = fetch_release_date_via_api(tralbum_id, band_id)
        if parsed:
            return parsed

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
# ПАРСИНГ ЖАНРОВ / ТЕГОВ BANDCAMP
# ==============================================================================
def fetch_from_genre(genre):
    genre_clean = genre.strip().lower().replace(" ", "-")
    if not genre_clean:
        return []

    print(f" 🏷️ [ЖАНР]: Ищем релизы по жанру/тегу '{genre_clean}'...")
    raw_items = []
    seen_links = set()

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"https://bandcamp.com/tag/{genre_clean}",
        "Origin": "https://bandcamp.com",
        "X-Requested-With": "XMLHttpRequest"
    }

    # --- СПОСОБ 1: GET API Bandcamp Discover ---
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
                if res.status_code == 200 and is_valid_html_response(res.text):
                    res_text = res.text
            except Exception:
                pass

        if not res_text:
            try:
                res = requests.get(disc_url, headers=headers, timeout=20)
                if res.status_code == 200 and is_valid_html_response(res.text):
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
                                "genre": genre_clean
                            })
            except Exception:
                pass

    # --- СПОСОБ 2: POST API Bandcamp Dig Deeper ---
    if not raw_items:
        api_url = "https://bandcamp.com/api/hub/2/dig_deeper"
        payload = {
            "filters": {
                "format": "all",
                "location": 0,
                "sort": "date",
                "tag_slug": genre_clean
            },
            "page": 1
        }
        post_headers = headers.copy()
        post_headers["Content-Type"] = "application/json"

        res_text = ""
        if CURL_CFFI_AVAILABLE:
            try:
                res = curl_requests.post(api_url, json=payload, headers=post_headers, impersonate="chrome120", timeout=20)
                if res.status_code == 200 and is_valid_html_response(res.text):
                    res_text = res.text
            except Exception:
                pass

        if not res_text:
            try:
                res = requests.post(api_url, json=payload, headers=post_headers, timeout=20)
                if res.status_code == 200 and is_valid_html_response(res.text):
                    res_text = res.text
            except Exception:
                pass

        if res_text:
            try:
                data = json.loads(res_text)
                items = data.get("items", [])
                for item in items:
                    link = item.get("tralbum_url") or item.get("link") or item.get("url")
                    title = item.get("title")
                    artist = item.get("artist_name") or item.get("artist") or "Неизвестный артист"
                    art_id = item.get("art_id")

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
                                "genre": genre_clean
                            })
            except Exception:
                pass

    # --- СПОСОБ 3: Парсинг HTML-страницы https://bandcamp.com/tag/<genre> ---
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
                                    "genre": genre_clean
                                })
                except Exception as e:
                    print(f"   ⚠️ Ошибка извлечения data-blob: {e}")

            if not raw_items:
                found_links = re.findall(
                    r'https://[a-zA-Z0-9\-_]+\.bandcamp\.com/(?:album|track)/[a-zA-Z0-9\-_]+',
                    html_text, re.IGNORECASE
                )
                for full_link in found_links:
                    clean_link = full_link.split('?')[0]
                    if clean_link not in seen_links:
                        seen_links.add(clean_link)
                        slug = clean_link.split("/")[-1]
                        title_slug = slug.replace("-", " ").title()
                        raw_items.append({
                            "title_full": f"{title_slug}",
                            "artist": "Various Artists",
                            "album_title": title_slug,
                            "image": "",
                            "description": f"New release in #{genre_clean}.",
                            "tags": [genre_clean],
                            "link": clean_link,
                            "genre": genre_clean
                        })

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


def fetch_from_artist_or_label(target):
    subdomain = extract_subdomain(target)
    if not subdomain:
        return []

    url = f"https://{subdomain}.bandcamp.com/music"
    headers = get_headers()
    print(f" 👤 [АРТИСТ/ЛЕЙБЛ]: Проверяем каталог {subdomain} ({url})...")

    html_text = ""
    if CURL_CFFI_AVAILABLE:
        try:
            res = curl_requests.get(url, headers=headers, impersonate="chrome120", timeout=30)
            if res.status_code == 200 and is_valid_html_response(res.text):
                html_text = res.text
        except Exception as e:
            print(f"   ⚠️ Ошибка запроса к артисту {subdomain} (curl_cffi): {e}")

    if not html_text:
        try:
            res = requests.get(url, headers=headers, timeout=20)
            if res.status_code == 200 and is_valid_html_response(res.text):
                html_text = res.text
        except Exception as e:
            print(f"   ⚠️ Ошибка requests к артисту {subdomain}: {e}")

    if not html_text:
        print(f"   ⚠️ Не удалось получить страницу артиста {subdomain}")
        return []

    artist_match = re.search(r'<meta\s+property="og:site_name"\s+content="([^"]+)"', html_text, re.IGNORECASE)
    if not artist_match:
        artist_match = re.search(r'<title>([^<]+)</title>', html_text, re.IGNORECASE)
    artist_name = artist_match.group(1).split('|')[0].strip() if artist_match else subdomain

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
                            "genre": "artist/label"
                        }
        except Exception as e:
            print(f"   ⚠️ Ошибка чтения data-client-items: {e}")

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
                "genre": "artist/label"
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
                    "genre": "artist/label"
                }

    raw_items = list(releases_map.values())
    print(f"   Найдено всего релизов в каталоге {artist_name}: {len(raw_items)}")
    return raw_items


# ==============================================================================
# ОТПРАВКА В TELEGRAM
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
    for release in new_releases:
        print(f"   🔎 Запрос даты: {release['title_full']}...")
        release["release_date"] = fetch_release_date(release["link"])
        if release["release_date"]:
            print(f"      ✅ Найдена: {release['release_date']}")
        else:
            print(f"      ❌ Не найдена")
        time.sleep(1)

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
