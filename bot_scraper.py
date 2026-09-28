import os
import json
import html
import time
import csv
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import requests

try:
    from dateutil import parser as dateutil_parser
    DATEUTIL_AVAILABLE = True
except ImportError:
    DATEUTIL_AVAILABLE = False

try:
    from curl_cffi import requests as curl_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False

# ==============================================================================
# НАСТРОЙКИ
# ==============================================================================
# 1. Жанры для поиска
GENRES = ["ambient"]

# 2. Артисты и лейблы для отслеживания (поддомен или полная ссылка)
TARGET_ARTISTS_AND_LABELS = [
    # "carbonbasedlifeforms",
    # "https://ultimae.bandcamp.com"
]

# 3. ФИЛЬТР ПО ВРЕМЕНИ ВЫПУСКА (в часах)
# Например: 1 = искать только релизы за последний час
# 24 = искать релизы за последние сутки
# None или 0 = отключить фильтрацию по времени (постить всё подряд)
MAX_RELEASE_AGE_HOURS = 24   

# 4. Скорость и лимиты
DISCOVER_ITEMS_LIMIT = 10   # Сколько первых (самых свежих) элементов из Discover API проверять
MAX_POSTS_PER_RUN = 3       # Лимит постов в Telegram за 1 запуск

POSTED_FILE = "posted_releases.json"
CSV_FILE = "releases_data.csv"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@bc_ambient")
SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY")

def get_headers():
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,json;q=0.8,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "X-Requested-With": "XMLHttpRequest"
    }

# ==============================================================================
# РАБОТА С ДАТАМИ И ВРЕМЕНЕМ
# ==============================================================================
def parse_datetime_str(val):
    """Универсальный парсер для дат любого типа (timestamp, ISO, RFC, текста)."""
    if not val:
        return None

    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val

    # Если целое число или float (timestamp)
    if isinstance(val, (int, float)):
        try:
            ts = float(val)
            if ts > 1e11:  # миллисекунды
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except Exception:
            return None

    val_str = str(val).strip()
    if not val_str:
        return None

    # Если строка содержит только цифры (timestamp)
    if val_str.isdigit():
        try:
            ts = float(val_str)
            if ts > 1e11:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except Exception:
            pass

    # 1. Попытка через dateutil.parser (если библиотека установлена)
    if DATEUTIL_AVAILABLE:
        try:
            dt = dateutil_parser.parse(val_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            pass

    # 2. Попытка через email.utils (для RFC 2822 / Bandcamp GMT дат)
    try:
        dt = parsedate_to_datetime(val_str)
        if dt:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
    except Exception:
        pass

    # 3. Попытка через datetime.fromisoformat (для ISO 8601)
    try:
        clean_iso = val_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        pass

    # 4. Попытка через наиболее распространенные шаблоны strptime
    formats = [
        "%d %b %Y %H:%M:%S %Z",  # 20 Sep 2026 00:00:00 GMT
        "%d %b %Y %H:%M:%S",
        "%B %d, %Y",             # September 20, 2026
        "%b %d, %Y",             # Sep 20, 2026
        "%d %B %Y",              # 20 September 2026
        "%Y%m%d",                # 20260920
        "%Y-%m-%d",              # 2026-09-20
        "%Y-%m-%d %H:%M:%S",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(val_str, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue

    return None

def is_release_new(release_dt):
    """Проверяет, входит ли дата релиза в допустимое окно времени (MAX_RELEASE_AGE_HOURS)."""
    if not MAX_RELEASE_AGE_HOURS or MAX_RELEASE_AGE_HOURS <= 0:
        return True  # Фильтр отключен

    if not release_dt:
        return False  # Пропускаем, если дата так и не определилась

    now = datetime.now(timezone.utc)
    
    if release_dt.tzinfo is None:
        release_dt = release_dt.replace(tzinfo=timezone.utc)

    age_seconds = (now - release_dt).total_seconds()
    max_age_seconds = MAX_RELEASE_AGE_HOURS * 3600

    return 0 <= age_seconds <= max_age_seconds

def get_release_date_from_url(url):
    """Достает точную дату публикации со страницы альбома/трека Bandcamp."""
    headers = get_headers()
    html_text = ""

    if CURL_CFFI_AVAILABLE:
        try:
            res = curl_requests.get(url, headers=headers, impersonate="chrome120", timeout=15)
            if res.status_code == 200:
                html_text = res.text
        except Exception:
            pass

    if not html_text:
        try:
            res = requests.get(url, headers=headers, timeout=15)
            if res.status_code == 200:
                html_text = res.text
        except Exception:
            return None

    if not html_text:
        return None

    # 1. Поиск и разбор JSON-LD (schema.org)
    json_ld_blocks = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html_text,
        re.DOTALL | re.IGNORECASE
    )
    for block in json_ld_blocks:
        try:
            data = json.loads(block.strip())
            items_to_check = data if isinstance(data, list) else [data]
            for item in items_to_check:
                if isinstance(item, dict):
                    raw_date = (
                        item.get("datePublished")
                        or item.get("dateCreated")
                        or item.get("dateModified")
                        or item.get("releaseDate")
                    )
                    parsed = parse_datetime_str(raw_date)
                    if parsed:
                        return parsed
        except Exception:
            pass

    # 2. Поиск JS-переменных (TrAlbumData и другие объекты)
    js_patterns = [
        r'publish_date\s*:\s*["\']([^"\']+)["\']',
        r'release_date\s*:\s*["\']([^"\']+)["\']',
        r'album_release_date\s*:\s*["\']([^"\']+)["\']',
        r'publish_date\s*:\s*(\d{10})',
        r'release_date\s*:\s*(\d{10})'
    ]
    for pattern in js_patterns:
        match = re.search(pattern, html_text)
        if match:
            parsed = parse_datetime_str(match.group(1))
            if parsed:
                return parsed

    # 3. Поиск метатегов
    meta_patterns = [
        r'itemprop=["\']datePublished["\']\s+content=["\']([^"\']+)["\']',
        r'content=["\']([^"\']+)["\']\s+itemprop=["\']datePublished["\']',
        r'property=["\']music:release_date["\']\s+content=["\']([^"\']+)["\']',
        r'name=["\']date["\']\s+content=["\']([^"\']+)["\']'
    ]
    for pattern in meta_patterns:
        match = re.search(pattern, html_text, re.IGNORECASE)
        if match:
            parsed = parse_datetime_str(match.group(1))
            if parsed:
                return parsed

    # 4. Поиск текстовых упоминаний даты релиза
    text_patterns = [
        r'released\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})',
        r'released\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})'
    ]
    for pattern in text_patterns:
        match = re.search(pattern, html_text, re.IGNORECASE)
        if match:
            parsed = parse_datetime_str(match.group(1))
            if parsed:
                return parsed

    return None

# ==============================================================================
# РАБОТА С ХРАНИЛИЩАМИ (JSON & CSV)
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
# ПАРСИНГ ЖАНРОВ (DISCOVER API)
# ==============================================================================
def fetch_from_discover_api(genre_name):
    url = f"https://bandcamp.com/api/discover/3/get_web?g={genre_name}&s=date&p=0"
    headers = get_headers()
    headers["Referer"] = f"https://bandcamp.com/tag/{genre_name}"
    headers["Origin"] = "https://bandcamp.com"
    print(f"🎧 [ЖАНР]: Запрос к Discover API ({genre_name})...")

    if CURL_CFFI_AVAILABLE:
        try:
            res = curl_requests.get(url, headers=headers, impersonate="chrome120", timeout=30)
            if res.status_code == 200:
                items = res.json().get("items", [])
                if items:
                    print(f"    Успешно получен список. Найдено релизов: {len(items)}")
                    return items
        except Exception as e:
            print(f"   ⚠️ Ошибка Discover API (curl_cffi): {e}")

    if SCRAPERAPI_KEY:
        try:
            scraper_url = f"http://api.scraperapi.com?api_key={SCRAPERAPI_KEY}&url={url}"
            res = requests.get(scraper_url, headers=headers, timeout=40)
            if res.status_code == 200:
                items = res.json().get("items", [])
                if items:
                    print(f"    Успешно через ScraperAPI! Найдено релизов: {len(items)}")
                    return items
        except Exception as e:
            print(f"   ⚠️ Ошибка Discover API (ScraperAPI): {e}")

    return []

def parse_item_details(item, target_genre, posted_set):
    if not isinstance(item, dict):
        return None

    # Формирование нормализованной ссылки
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

    link = link.replace(".bandcamp.com/a/", ".bandcamp.com/album/")
    link = link.replace(".bandcamp.com/t/", ".bandcamp.com/track/")

    # Быстрый пропуск: если уже был опубликован, лишний запрос даты не делаем
    if link in posted_set:
        return {"link": link, "already_posted": True}

    # Извлечение даты
    release_dt = None
    pub_val = (
        item.get("publish_date")
        or item.get("released")
        or item.get("publish_date_num")
        or item.get("release_date")
    )
    if pub_val:
        release_dt = parse_datetime_str(pub_val)

    # Если в API даты не оказалось или распарсить не удалось, загружаем её со страницы
    if not release_dt:
        release_dt = get_release_date_from_url(link)

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

    art_id = item.get("art_id") or item.get("primary_art_id") or item.get("image_id")
    image_url = f"https://f4.bcbits.com/img/a{art_id}_10.jpg" if art_id else ""

    genre_text = item.get("genre_text", target_genre)
    tags = [target_genre]
    if genre_text and genre_text.lower() != target_genre.lower():
        tags.append(genre_text.lower())

    return {
        "title_full": f"{title} by {artist}",
        "artist": artist,
        "album_title": title,
        "image": image_url,
        "description": f"New {target_genre} release from {artist}.",
        "tags": tags,
        "link": link,
        "genre": target_genre,
        "release_dt": release_dt,
        "already_posted": False
    }

# ==============================================================================
# ПАРСИНГ АРТИСТОВ / ЛЕЙБЛОВ
# ==============================================================================
def extract_subdomain(target):
    target = target.strip().lower()
    target = re.sub(r'^https?://', '', target)
    target = target.split('.')[0]
    target = target.split('/')[0]
    return target

def fetch_from_artist_or_label(target, posted_set):
    subdomain = extract_subdomain(target)
    if not subdomain:
        return []

    url = f"https://{subdomain}.bandcamp.com/music"
    headers = get_headers()
    print(f"👤 [АРТИСТ/ЛЕЙБЛ]: Проверяем {subdomain} ({url})...")

    html_text = ""
    if CURL_CFFI_AVAILABLE:
        try:
            res = curl_requests.get(url, headers=headers, impersonate="chrome120", timeout=30)
            if res.status_code == 200:
                html_text = res.text
        except Exception as e:
            print(f"   ⚠️ Ошибка запроса к артисту {subdomain}: {e}")

    if not html_text:
        try:
            res = requests.get(url, headers=headers, timeout=20)
            if res.status_code == 200:
                html_text = res.text
        except Exception as e:
            print(f"   ⚠️ Ошибка requests к артисту {subdomain}: {e}")
            return []

    if not html_text:
        return []

    artist_match = re.search(r'<meta\s+property="og:site_name"\s+content="([^"]+)"', html_text)
    if not artist_match:
        artist_match = re.search(r'<title>([^<]+)</title>', html_text)
    artist_name = artist_match.group(1).split('|')[0].strip() if artist_match else subdomain

    grid_items = re.findall(
        r'<a\s+href="(/(?:album|track|a|t)/[^"]+)"[^>]*>.*?<p\s+class="title"[^>]*>\s*([^<]+)\s*</p>',
        html_text,
        re.DOTALL
    )

    art_ids = re.findall(r'f4\.bcbits\.com/img/a(\d+)_\d+\.jpg', html_text)
    items = []
    seen_links = set()

    for idx, (path, title) in enumerate(grid_items[:3]):
        clean_title = html.unescape(title.strip())
        full_link = f"https://{subdomain}.bandcamp.com{path}"
        full_link = full_link.replace(".bandcamp.com/a/", ".bandcamp.com/album/")
        full_link = full_link.replace(".bandcamp.com/t/", ".bandcamp.com/track/")

        if full_link in seen_links:
            continue
        seen_links.add(full_link)

        if full_link in posted_set:
            continue

        release_dt = get_release_date_from_url(full_link)
        img_url = f"https://f4.bcbits.com/img/a{art_ids[idx]}_10.jpg" if idx < len(art_ids) else ""

        items.append({
            "title_full": f"{clean_title} by {artist_name}",
            "artist": artist_name,
            "album_title": clean_title,
            "image": img_url,
            "description": f"New release from {artist_name}.",
            "tags": [subdomain],
            "link": full_link,
            "genre": "artist/label",
            "release_dt": release_dt,
            "already_posted": False
        })

    print(f"    Найдено непубликовавшихся релизов у {artist_name}: {len(items)}")
    return items

# ==============================================================================
# ОТПРАВКА В TELEGRAM
# ==============================================================================
def send_to_telegram(release):
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"

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

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "photo": release["image"],
        "caption": caption,
        "parse_mode": "HTML"
    }

    resp = requests.post(api_url, data=payload, timeout=20)
    return resp.ok

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
        for item in raw_items[:DISCOVER_ITEMS_LIMIT]:
            details = parse_item_details(item, genre, posted)
            if details and details["link"] and details["link"] not in seen_links:
                seen_links.add(details["link"])
                if not details.get("already_posted"):
                    releases.append(details)

    # 2. Собираем релизы по артистам/лейблам
    for target in TARGET_ARTISTS_AND_LABELS:
        artist_items = fetch_from_artist_or_label(target, posted)
        for details in artist_items:
            if details and details["link"] and details["link"] not in seen_links:
                seen_links.add(details["link"])
                releases.append(details)

    print(f"🔎 Новых (ранее не отправленных) релизов для проверки даты: {len(releases)}")

    # 3. Фильтрация по дате выпуска
    new_releases = []
    for r in releases:
        rel_date = r.get("release_dt")
        if is_release_new(rel_date):
            new_releases.append(r)
        else:
            date_str = rel_date.strftime("%Y-%m-%d %H:%M UTC") if rel_date else "Не удалось определить"
            print(f"⏳ Пропущен устаревший релиз: {r['title_full']} (Дата: {date_str})")

    print(f"✨ Релизов, вышедших за последние {MAX_RELEASE_AGE_HOURS} ч.: {len(new_releases)}")

    new_posts = 0
    for release in reversed(new_releases):
        if new_posts >= MAX_POSTS_PER_RUN:
            print(f"🛑 Достигнут лимит в {MAX_POSTS_PER_RUN} постов за запуск. Остановка.")
            break

        print(f"🚀 Публикация: {release['title_full']}")
        if send_to_telegram(release):
            posted.add(release["link"])
            save_to_csv(release)
            new_posts += 1
            time.sleep(3)
        else:
            print(f"❌ Не удалось отправить в Telegram: {release['link']}")

    save_posted(posted)
    print(f"🏁 Завершено. Опубликовано новых релизов: {new_posts}")

if __name__ == "__main__":
    main()
```eof
