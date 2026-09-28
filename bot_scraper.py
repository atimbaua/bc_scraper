import os
import json
import html
import time
import csv
import re
from datetime import datetime
import requests

try:
    from curl_cffi import requests as curl_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False

# ==============================================================================
# НАСТРОЙКИ
# ==============================================================================
# 1. Жанры для поиска (можно указать один или несколько: ["ambient", "post-rock"])
GENRES = [
    # "ambient"
]

# 2. Артисты и лейблы для отслеживания (указывайте поддомен или ссылку)
# Примеры: "carbonbasedlifeforms", "https://ultimae.bandcamp.com", "solarfields"
TARGET_ARTISTS_AND_LABELS = [
    # "https://sessionvictim.bandcamp.com"
]

MAX_POSTS_PER_RUN = 3       # Лимит постов за 1 запуск
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
# РАБОТА С ССЫЛКАМИ И ФАЙЛАМИ (JSON & CSV)
# ==============================================================================
def init_csv_file():
    """Создает CSV файл с точным набором указанных колонок."""
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
    """Сохраняет релиз строго по нужным колонкам."""
    published_at_utc = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
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
# ПАРСИНГ ЖАНРОВ (DISCOVER / HUB API)
# ==============================================================================
def fetch_from_discover_api(genre_name):
    url = f"https://bandcamp.com/api/discover/3/get_web?g={genre_name}&s=date&p=0"
    headers = get_headers()
    headers["Referer"] = f"https://bandcamp.com/tag/{genre_name}"
    headers["Origin"] = "https://bandcamp.com"
    print(f" 🎧 [ЖАНР]: Пробуем Discover API ({genre_name})...")

    if CURL_CFFI_AVAILABLE:
        try:
            res = curl_requests.get(url, headers=headers, impersonate="chrome120", timeout=30)
            if res.status_code == 200:
                items = res.json().get("items", [])
                if items:
                    print(f"    Успех Discover API! Найдено релизов: {len(items)}")
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
                    print(f"    Успех Discover API (ScraperAPI)! Найдено релизов: {len(items)}")
                    return items
        except Exception as e:
            print(f"   ⚠️ Ошибка Discover API (ScraperAPI): {e}")

    return []

def parse_item_details(item, target_genre):
    if not isinstance(item, dict):
        return None

    # Ссылка
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

    # Заменяем алиасы /a/ и /t/ на полные пути /album/ и /track/
    link = link.replace(".bandcamp.com/a/", ".bandcamp.com/album/")
    link = link.replace(".bandcamp.com/t/", ".bandcamp.com/track/")

    # Артист
    artist = (
        item.get("band_name")
        or item.get("artist")
        or item.get("artist_name")
        or item.get("secondary_text")
        or "Неизвестный исполнитель"
    )
    if isinstance(artist, str) and artist.startswith("by "):
        artist = artist[3:]

    # Название
    title = (
        item.get("title")
        or item.get("album_title")
        or item.get("primary_text")
        or f"{target_genre.capitalize()} Release"
    )

    title_full = f"{title} by {artist}"

    # Обложка
    art_id = item.get("art_id") or item.get("primary_art_id") or item.get("image_id")
    image_url = f"https://f4.bcbits.com/img/a{art_id}_10.jpg" if art_id else ""

    genre_text = item.get("genre_text", target_genre)
    tags = [target_genre]
    if genre_text and genre_text.lower() != target_genre.lower():
        tags.append(genre_text.lower())

    return {
        "title_full": title_full,
        "artist": artist,
        "album_title": title,
        "image": image_url,
        "description": f"New {target_genre} release from {artist}.",
        "tags": tags,
        "link": link,
        "genre": target_genre
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

def fetch_from_artist_or_label(target):
    subdomain = extract_subdomain(target)
    if not subdomain:
        return []

    url = f"https://{subdomain}.bandcamp.com/music"
    headers = get_headers()
    print(f" 👤 [АРТИСТ/ЛЕЙБЛ]: Проверяем {subdomain} ({url})...")

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

    # Определяем название артиста/лейбла из мета-тегов
    artist_match = re.search(r'<meta\s+property="og:site_name"\s+content="([^"]+)"', html_text)
    if not artist_match:
        artist_match = re.search(r'<title>([^<]+)</title>', html_text)
    artist_name = artist_match.group(1).split('|')[0].strip() if artist_match else subdomain

    items = []
    # Находим альбомы и треки на странице
    grid_items = re.findall(
        r'<a\s+href="(/(?:album|track|a|t)/[^"]+)"[^>]*>.*?<p\s+class="title"[^>]*>\s*([^<]+)\s*</p>',
        html_text,
        re.DOTALL
    )

    # Если релиза всего 1 (страница автоматически открывает плеер)
    if not grid_items:
        og_url = re.search(r'<meta\s+property="og:url"\s+content="([^"]+)"', html_text)
        og_title = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html_text)
        if og_url and og_title:
            link = og_url.group(1).replace(".bandcamp.com/a/", ".bandcamp.com/album/").replace(".bandcamp.com/t/", ".bandcamp.com/track/")
            title = html.unescape(og_title.group(1).strip())
            og_img = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html_text)
            img = og_img.group(1) if og_img else ""
            
            items.append({
                "title_full": f"{title} by {artist_name}",
                "artist": artist_name,
                "album_title": title,
                "image": img,
                "description": f"New release from {artist_name}.",
                "tags": [subdomain],
                "link": link,
                "genre": "artist/label"
            })
            return items

    # Изображения обложек
    art_ids = re.findall(r'f4\.bcbits\.com/img/a(\d+)_\d+\.jpg', html_text)

    seen_links = set()
    for idx, (path, title) in enumerate(grid_items):
        clean_title = html.unescape(title.strip())
        full_link = f"https://{subdomain}.bandcamp.com{path}"
        full_link = full_link.replace(".bandcamp.com/a/", ".bandcamp.com/album/")
        full_link = full_link.replace(".bandcamp.com/t/", ".bandcamp.com/track/")

        if full_link in seen_links:
            continue
        seen_links.add(full_link)

        img_url = f"https://f4.bcbits.com/img/a{art_ids[idx]}_10.jpg" if idx < len(art_ids) else ""

        items.append({
            "title_full": f"{clean_title} by {artist_name}",
            "artist": artist_name,
            "album_title": clean_title,
            "image": img_url,
            "description": f"New release from {artist_name}.",
            "tags": [subdomain],
            "link": full_link,
            "genre": "artist/label"
        })

    print(f"    Найдено релизов у {artist_name}: {len(items)}")
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

    print(f"🔎 Всего распознано уникальных релизов: {len(releases)}")

    new_releases = [r for r in releases if r["link"] not in posted]
    print(f"✨ Новых релизов для публикации: {len(new_releases)}")

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
