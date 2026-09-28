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
def _try_parse_date_string(d_str):
    if not d_str:
        return None
    # Очищаем от времени (например, " 00:00:00 GMT" или "T00:00:00Z")
    d_clean = re.sub(r'\s+\d{2}:\d{2}:\d{2}.*', '', d_str).strip()
    d_clean = d_clean.replace("Z", "").strip()

    formats = (
        "%d %b %Y", "%d %B %Y",
        "%B %d, %Y", "%b %d, %Y",
        "%Y-%m-%d", "%Y/%m/%d"
    )
    for fmt in formats:
        try:
            return datetime.strptime(d_clean[:11].strip(), fmt).date()
        except ValueError:
            pass

    # Поиск шаблона "DD Month YYYY" внутри строки
    match_dmy = re.search(r'(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})', d_str)
    if match_dmy:
        sub_str = match_dmy.group(1)
        for fmt in ("%d %b %Y", "%d %B %Y"):
            try:
                return datetime.strptime(sub_str, fmt).date()
            except ValueError:
                pass

    # Поиск шаблона "Month DD, YYYY"
    match_mdy = re.search(r'([A-Za-z]+\s+\d{1,2},\s+\d{4})', d_str)
    if match_mdy:
        sub_str = match_mdy.group(1)
        for fmt in ("%B %d, %Y", "%b %d, %Y"):
            try:
                return datetime.strptime(sub_str, fmt).date()
            except ValueError:
                pass

    return None

def parse_date_from_html(html_text):
    if not html_text:
        return None

    clean_html = re.sub(r'\s+', ' ', html_text)

    # 0. Поиск в атрибуте data-tralbum (Главный источник дат на Bandcamp альбомах/треках)
    tralbum_attr_match = re.search(r'data-tralbum="([^"]+)"', html_text)
    if tralbum_attr_match:
        try:
            tr_json = json.loads(html.unescape(tralbum_attr_match.group(1)))
            # Проверяем ключи дат в корне и внутри словаря "current"
            for key in ["release_date", "current_release_date", "original_release_date"]:
                if key in tr_json and tr_json[key]:
                    parsed = _try_parse_date_string(str(tr_json[key]))
                    if parsed:
                        return parsed
            if "current" in tr_json and isinstance(tr_json["current"], dict):
                for key in ["release_date", "current_release_date"]:
                    if key in tr_json["current"] and tr_json["current"][key]:
                        parsed = _try_parse_date_string(str(tr_json["current"][key]))
                        if parsed:
                            return parsed
        except Exception:
            pass

    # 1. Поиск в JSON-LD (<script type="application/ld+json">)
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
                date_str = item.get("datePublished") or item.get("releaseDate")
                if date_str:
                    parsed = _try_parse_date_string(str(date_str))
                    if parsed:
                        return parsed
        except Exception:
            pass

    # 2. Поиск в JS-переменных TrAlbumData
    tr_matches = re.findall(r'(?:album_release_date|release_date|current_release_date)"?\s*:\s*"([^"]+)"', clean_html, re.IGNORECASE)
    for raw_date_str in tr_matches:
        parsed = _try_parse_date_string(raw_date_str)
        if parsed:
            return parsed

    # 3. Поиск стандартного текста "released Month DD, YYYY" / "releases Month DD, YYYY"
    date_match = re.search(r'(?:released|releases)\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})', clean_html, re.IGNORECASE)
    if date_match:
        parsed = _try_parse_date_string(date_match.group(1))
        if parsed:
            return parsed

    # 4. Поиск "released DD Month YYYY"
    date_match_alt = re.search(r'(?:released|releases)\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})', clean_html, re.IGNORECASE)
    if date_match_alt:
        parsed = _try_parse_date_string(date_match_alt.group(1))
        if parsed:
            return parsed

    # 5. Поиск itemprop="datePublished"
    meta_dp = re.search(r'itemprop="datePublished"\s+content="([^"]+)"', clean_html, re.IGNORECASE)
    if meta_dp:
        parsed = _try_parse_date_string(meta_dp.group(1))
        if parsed:
            return parsed

    return None
    
def fetch_release_date(url):
    headers = get_headers()
    html_text = ""

    # 1. Попытка через curl_cffi (обход Cloudflare)
    if CURL_CFFI_AVAILABLE:
        try:
            res = curl_requests.get(url, headers=headers, impersonate="chrome120", timeout=30)
            if res.status_code == 200 and is_valid_html_response(res.text):
                html_text = res.text
        except Exception as e:
            print(f"   ⚠️ Ошибка fetch_release_date (curl_cffi): {e}")

    # 2. Резервная попытка через стандартный requests
    if not html_text:
        try:
            res = requests.get(url, headers=headers, timeout=20)
            if res.status_code == 200 and is_valid_html_response(res.text):
                html_text = res.text
        except Exception as e:
            print(f"   ⚠️ Ошибка fetch_release_date (requests): {e}")

    return parse_date_from_html(html_text)

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
        html_headers = get_headers()
        html_text = ""

        if CURL_CFFI_AVAILABLE:
            try:
                res = curl_requests.get(tag_url, headers=html_headers, impersonate="chrome120", timeout=20)
                if res.status_code == 200 and is_valid_html_response(res.text):
                    html_text = res.text
            except Exception:
                pass

        if not html_text:
            try:
                res = requests.get(tag_url, headers=html_headers, timeout=20)
                if res.status_code == 200 and is_valid_html_response(res.text):
                    html_text = res.text
            except Exception:
                pass

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
                found_links = re.findall(r'https://[a-zA-Z0-9\-_]+\.bandcamp\.com/(?:album|track)/[a-zA-Z0-9\-_]+', html_text, re.IGNORECASE)
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

    caption = (
        f"🌌 <b>{title}</b>\n\n"
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
