import os
import json
import html
import time
import csv
import re
import urllib.parse
from datetime import datetime, timezone
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
    "https://windyandcarl.bandcamp.com"
]

MAX_DAYS_AGO = 30        # Публиковать релизы не старше N дней
ALLOW_UPCOMING = True    # Публиковать предзаказы / анонсы

MAX_POSTS_PER_RUN = 10
POSTED_FILE = "posted_releases.json"
CSV_FILE = "releases_data.csv"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@bc_ambient")
SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY")

def get_headers():
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

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
# ПАРСИНГ ДАТЫ И ПРОВЕРКА КРИТЕРИЕВ
# ==============================================================================
def parse_date_str(date_val):
    if not date_val:
        return None
    if isinstance(date_val, datetime):
        return date_val.date()
    
    # Если дата передана числовым timestamp (в секундах или миллисекундах)
    if isinstance(date_val, (int, float)):
        if date_val > 1e11:
            date_val /= 1000
        return datetime.fromtimestamp(date_val, tz=timezone.utc).date()

    # Парсинг ISO / текстовых строк
    d_str = str(date_val).strip()
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(d_str, fmt).date()
        except ValueError:
            pass

    try:
        if "T" in d_str:
            return datetime.fromisoformat(d_str.replace("Z", "+00:00")).date()
    except Exception:
        pass

    return None

def is_release_date_valid(release_date):
    if release_date is None:
        print("   ⚠️ Дата релиза не найдена на странице, публикация по умолчанию.")
        return True

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
# ПАРСИНГ ЖАНРОВ (DISCOVER API)
# ==============================================================================
def fetch_from_discover_api(genre_name):
    clean_genre = urllib.parse.quote(genre_name.strip())
    target_url = f"https://bandcamp.com/api/discover/3/get_web?g={clean_genre}&s=date&p=0"
    headers = get_headers()
    headers["Referer"] = f"https://bandcamp.com/tag/{clean_genre}"
    headers["Origin"] = "https://bandcamp.com"
    print(f" 🎧 [ЖАНР]: Пробуем Discover API ({genre_name})...")

    if SCRAPERAPI_KEY:
        try:
            clean_key = SCRAPERAPI_KEY.strip().strip('"').strip("'")
            encoded_target = urllib.parse.quote(target_url, safe='')
            scraper_url = f"http://api.scraperapi.com?api_key={clean_key}&url={encoded_target}"
            res = requests.get(scraper_url, timeout=30)
            if res.status_code == 200:
                try:
                    data = res.json()
                    items = data.get("items", [])
                    if items:
                        print(f"    Успех Discover API (ScraperAPI)! Найдено релизов: {len(items)}")
                        return items
                except json.JSONDecodeError:
                    print("   ⚠️ ScraperAPI вернул HTML вместо JSON")
        except Exception as e:
            print(f"   ⚠️ Ошибка Discover API (ScraperAPI): {e}")

    if CURL_CFFI_AVAILABLE:
        try:
            res = curl_requests.get(target_url, headers=headers, impersonate="chrome120", timeout=30)
            if res.status_code == 200:
                try:
                    data = res.json()
                    items = data.get("items", [])
                    if items:
                        print(f"    Успех Discover API (curl_cffi)! Найдено релизов: {len(items)}")
                        return items
                except json.JSONDecodeError:
                    print("   ⚠️ curl_cffi заблокирован Cloudflare")
        except Exception as e:
            print(f"   ⚠️ Ошибка Discover API (curl_cffi): {e}")

    try:
        res = requests.get(target_url, headers=headers, timeout=20)
        if res.status_code == 200:
            try:
                data = res.json()
                items = data.get("items", [])
                if items:
                    print(f"    Успех Discover API (requests)! Найдено релизов: {len(items)}")
                    return items
            except json.JSONDecodeError:
                print("   ⚠️ Прямой requests заблокирован Cloudflare")
    except Exception as e:
        print(f"   ⚠️ Ошибка Discover API (requests): {e}")

    return []

def parse_item_details(item, target_genre):
    if not isinstance(item, dict):
        return None

    link = (
        item.get("tralbum_url")
        or item.get("item_url")
        or item.get("page_url")
        or item.get("url")
        or item.get("link")
    )

    if not link and isinstance(item.get("url_hints"), dict):
        hints = item["url_hints"]
        subdomain = hints.get("subdomain")
        slug = hints.get("slug")
        raw_type = hints.get("item_type") or hints.get("type") or item.get("type")
        item_type = "album" if raw_type == "a" else ("track" if raw_type == "t" else "album")
        if subdomain and slug:
            link = f"https://{subdomain}.bandcamp.com/{item_type}/{slug}"

    if not link and item.get("subdomain") and item.get("slug"):
        subdomain = item["subdomain"]
        slug = item["slug"]
        raw_type = item.get("type")
        item_type = "album" if raw_type == "a" else ("track" if raw_type == "t" else "album")
        link = f"https://{subdomain}.bandcamp.com/{item_type}/{slug}"

    if not link:
        return None

    if link.startswith("//"):
        link = "https:" + link

    link = link.replace(".bandcamp.com/a/", ".bandcamp.com/album/").replace(".bandcamp.com/t/", ".bandcamp.com/track/")

    artist = (
        item.get("band_name")
        or item.get("artist")
        or item.get("artist_name")
        or item.get("secondary_text")
        or "Неизвестный исполнитель"
    )
    if isinstance(artist, str) and artist.startswith("by "):
        artist = artist[3:]

    title = (
        item.get("title")
        or item.get("album_title")
        or item.get("primary_text")
        or f"{target_genre.capitalize()} Release"
    )

    title_full = f"{title} by {artist}"
    art_id = item.get("art_id") or item.get("primary_art_id") or item.get("image_id")
    image_url = f"https://f4.bcbits.com/img/a{art_id}_10.jpg" if art_id else ""

    pub_date_raw = item.get("publish_date") or item.get("release_date") or item.get("published")

    return {
        "title_full": title_full,
        "artist": artist,
        "album_title": title,
        "image": image_url,
        "description": f"New {target_genre} release from {artist}.",
        "tags": [target_genre],
        "link": link,
        "genre": target_genre,
        "release_date": parse_date_str(pub_date_raw)
    }

# ==============================================================================
# ПАРСИНГ АРТИСТОВ / ЛЕЙБЛОВ (ЧЕРЕЗ Скрытый BAND DATA API)
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
    print(f" 👤 [АРТИСТ/ЛЕЙБЛ]: Проверяем полный каталог {subdomain} ({url})...")

    html_text = ""
    if CURL_CFFI_AVAILABLE:
        try:
            res = curl_requests.get(url, headers=headers, impersonate="chrome120", timeout=30)
            if res.status_code == 200:
                html_text = res.text
        except Exception as e:
            print(f"   ⚠️ Ошибка запроса к артисту {subdomain} (curl_cffi): {e}")

    if not html_text:
        try:
            res = requests.get(url, headers=headers, timeout=20)
            if res.status_code == 200:
                html_text = res.text
        except Exception as e:
            print(f"   ⚠️ Ошибка requests к артисту {subdomain}: {e}")

    if not html_text and SCRAPERAPI_KEY:
        try:
            clean_key = SCRAPERAPI_KEY.strip().strip('"').strip("'")
            encoded_url = urllib.parse.quote(url, safe='')
            scraper_url = f"http://api.scraperapi.com?api_key={clean_key}&url={encoded_url}"
            res = requests.get(scraper_url, timeout=30)
            if res.status_code == 200:
                html_text = res.text
        except Exception as e:
            print(f"   ⚠️ Ошибка ScraperAPI к артисту {subdomain}: {e}")

    if not html_text:
        print(f"   ⚠️ Не удалось получить страницу артиста {subdomain}")
        return []

    artist_match = re.search(r'<meta\s+property="og:site_name"\s+content="([^"]+)"', html_text, re.IGNORECASE)
    if not artist_match:
        artist_match = re.search(r'<title>([^<]+)</title>', html_text, re.IGNORECASE)
    artist_name = artist_match.group(1).split('|')[0].strip() if artist_match else subdomain

    items = []
    seen_links = set()

    # 1. Извлечение ВСЕХ альбомов из встроенного JS-объекта data-band / TrAlbumData
    band_data_match = re.search(r'data-band="([^"]+)"', html_text) or re.search(r'data-embed="([^"]+)"', html_text)
    
    # Также ищем встроенные ссылки на дискографию через JS-переменные
    grid_json_matches = re.findall(r'(\{(?:[^{}]*?"title"[^{}]*?)\})', html_text)

    if band_data_match:
        try:
            raw_json = html.unescape(band_data_match.group(1))
            band_json = json.loads(raw_json)
            discog = band_json.get("discography", []) or band_json.get("grid_items", [])
            for disc in discog:
                path = disc.get("page_url") or disc.get("title_link") or disc.get("url")
                title = disc.get("title")
                art_id = disc.get("art_id") or disc.get("image_id")
                pub_date = disc.get("publish_date") or disc.get("release_date")
                
                if path and title:
                    full_link = f"https://{subdomain}.bandcamp.com{path}" if path.startswith("/") else path
                    full_link = full_link.replace(".bandcamp.com/a/", ".bandcamp.com/album/").replace(".bandcamp.com/t/", ".bandcamp.com/track/")
                    
                    if full_link not in seen_links and ("/album/" in full_link or "/track/" in full_link):
                        seen_links.add(full_link)
                        img_url = f"https://f4.bcbits.com/img/a{art_id}_10.jpg" if art_id else ""
                        items.append({
                            "title_full": f"{title} by {artist_name}",
                            "artist": artist_name,
                            "album_title": title,
                            "image": img_url,
                            "description": f"New release from {artist_name}.",
                            "tags": [subdomain],
                            "link": full_link,
                            "genre": "artist/label",
                            "release_date": parse_date_str(pub_date)
                        })
        except Exception as e:
            print(f"   ⚠️ Ошибка чтения data-band: {e}")

    # 2. Второй канал: извлечение ссылок напрямую из всех тегов <a> сетки
    all_album_links = re.findall(r'href="(/(?:album|track)/[a-zA-Z0-9\-_]+)"', html_text, re.IGNORECASE)
    for path in all_album_links:
        full_link = f"https://{subdomain}.bandcamp.com{path}"
        if full_link not in seen_links:
            seen_links.add(full_link)
            
            # Название из URL slug в качестве запасного варианта
            title_slug = path.split("/")[-1].replace("-", " ").title()
            
            # Поиск art_id рядом
            art_match = re.search(r'f4\.bcbits\.com/img/a(\d+)_\d+\.jpg', html_text)
            art_id = art_match.group(1) if art_match else ""
            img_url = f"https://f4.bcbits.com/img/a{art_id}_10.jpg" if art_id else ""

            items.append({
                "title_full": f"{title_slug} by {artist_name}",
                "artist": artist_name,
                "album_title": title_slug,
                "image": img_url,
                "description": f"New release from {artist_name}.",
                "tags": [subdomain],
                "link": full_link,
                "genre": "artist/label",
                "release_date": None
            })

    # СОРТИРОВКА ПО СВЕЖЕСТИ:
    # Ищем art_id из картинки (у новых релизов art_id всегда СУЩЕСТВЕННО больше!)
    def get_sort_key(item):
        # 1. Если есть распарсенная дата
        if item.get("release_date"):
            return item["release_date"].strftime("%Y%m%d")
        # 2. Иначе по цифре art_id из ссылки картинки
        match = re.search(r'/a(\d+)_\d+\.jpg', item.get("image", ""))
        if match:
            return match.group(1).zfill(12)
        return "000000000000"

    # Сортируем строго ОТ СВЕЖИХ К СТАРЫМ
    items.sort(key=get_sort_key, reverse=True)

    print(f"    Найдено ВСЕГО релизов у {artist_name}: {len(items)}")
    return items

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

    # 1. Собираем релизы по жанрам
    for genre in GENRES:
        raw_items = fetch_from_discover_api(genre)
        for item in raw_items:
            details = parse_item_details(item, genre)
            if details and details["link"] and details["link"] not in seen_links:
                seen_links.add(details["link"])
                releases.append(details)

    # 2. Собираем релизы по артистам/лейблам
    for target in TARGET_ARTISTS_AND_LABELS:
        artist_items = fetch_from_artist_or_label(target)
        for details in artist_items:
            if details and details["link"] and details["link"] not in seen_links:
                seen_links.add(details["link"])
                releases.append(details)

    print(f"\n🔎 Всего распознано уникальных релизов: {len(releases)}")

    new_releases = [r for r in releases if r["link"] not in posted]
    print(f"✨ Новых не опубликованных ранее релизов: {len(new_releases)}")

    new_posts = 0
    for release in new_releases:
        if new_posts >= MAX_POSTS_PER_RUN:
            print(f"🛑 Достигнут лимит в {MAX_POSTS_PER_RUN} постов за запуск. Остановка.")
            break

        print(f"\n🔍 Проверка даты для: {release['title_full']}")
        
        # Берем заранее распарсенную дату из API или делаем доп. запрос
        release_date = release.get("release_date")
        if not release_date:
            # Резервный вызов проверки отдельной страницы
            try:
                res = requests.get(release["link"], headers=get_headers(), timeout=15)
                if res.status_code == 200:
                    d_match = re.search(r'(?:released|releases)\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})', res.text, re.IGNORECASE)
                    if d_match:
                        release_date = parse_date_str(d_match.group(1))
            except Exception:
                pass

        if not is_release_date_valid(release_date):
            posted.add(release["link"])
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
