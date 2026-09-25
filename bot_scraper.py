import os
import json
import html
import time
import csv
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
GENRE = "ambient"          # Изменяйте жанр здесь (ambient, post-rock, techno и т.д.)
MAX_POSTS_PER_RUN = 3       # Лимит постов за 1 запуск
POSTED_FILE = "posted_releases.json"
CSV_FILE = "releases_data.csv"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@bc_ambient")
SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY")

def get_headers():
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"https://bandcamp.com/tag/{GENRE}",
        "Origin": "https://bandcamp.com"
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
    genre = GENRE
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
# ПАРСИНГ API BANDCAMP
# ==============================================================================
def fetch_from_discover_api():
    url = f"https://bandcamp.com/api/discover/3/get_web?g={GENRE}&s=date&p=0"
    headers = get_headers()
    print(f" [МЕТОД 1]: Пробуем GET Discover API ({GENRE})...")

    if CURL_CFFI_AVAILABLE:
        try:
            res = curl_requests.get(url, headers=headers, impersonate="chrome120", timeout=30)
            if res.status_code == 200:
                data = res.json()
                items = data.get("items", [])
                if items:
                    print(f"    Успех Discover API (curl_cffi)! Найдено релизов: {len(items)}")
                    return items
            print(f"   ⚠️ Discover API (curl_cffi): Код {res.status_code}")
        except Exception as e:
            print(f"   ⚠️ Ошибка Discover API (curl_cffi): {e}")

    if SCRAPERAPI_KEY:
        try:
            scraper_url = f"http://api.scraperapi.com?api_key={SCRAPERAPI_KEY}&url={url}"
            res = requests.get(scraper_url, headers=headers, timeout=40)
            if res.status_code == 200:
                data = res.json()
                items = data.get("items", [])
                if items:
                    print(f"    Успех Discover API (ScraperAPI)! Найдено релизов: {len(items)}")
                    return items
        except Exception as e:
            print(f"   ⚠️ Ошибка Discover API (ScraperAPI): {e}")

    return []

def fetch_from_hub_api():
    url = "https://bandcamp.com/api/hub/2/dig_deeper"
    headers = get_headers()
    payload = {
        "filters": {
            "format": "all",
            "location": 0,
            "sort": "date",
            "tags": [GENRE]
        },
        "page": 1
    }
    print(f" [МЕТОД 2]: Пробуем POST Dig Deeper API ({GENRE})...")

    if CURL_CFFI_AVAILABLE:
        try:
            res = curl_requests.post(url, json=payload, headers=headers, impersonate="chrome120", timeout=30)
            if res.status_code == 200:
                data = res.json()
                items = data.get("items", [])
                if items:
                    print(f"    Успех Dig Deeper API (curl_cffi)! Найдено релизов: {len(items)}")
                    return items
            print(f"   ⚠️ Dig Deeper API (curl_cffi): Код {res.status_code}")
        except Exception as e:
            print(f"   ⚠️ Ошибка Dig Deeper API (curl_cffi): {e}")

    return []

def parse_item_details(item):
    if not isinstance(item, dict):
        return None

    # 1. Извлечение первичной ссылки
    link = (
        item.get("tralbum_url")
        or item.get("item_url")
        or item.get("page_url")
        or item.get("url")
        or item.get("link")
    )

    # Если готового URL нет, собираем из url_hints
    if not link and isinstance(item.get("url_hints"), dict):
        hints = item["url_hints"]
        subdomain = hints.get("subdomain")
        slug = hints.get("slug")
        raw_type = hints.get("item_type") or hints.get("type") or item.get("type")
        item_type = "album" if raw_type == "a" else ("track" if raw_type == "t" else raw_type or "album")
        if subdomain and slug:
            link = f"https://{subdomain}.bandcamp.com/{item_type}/{slug}"

    # Если прямого URL нет, собираем из полей subdomain и slug
    if not link and item.get("subdomain") and item.get("slug"):
        subdomain = item["subdomain"]
        slug = item["slug"]
        raw_type = item.get("type")
        item_type = "album" if raw_type == "a" else ("track" if raw_type == "t" else "album")
        link = f"https://{subdomain}.bandcamp.com/{item_type}/{slug}"

    if not link:
        return None

    # 2. Нормализация формата ссылки
    if link.startswith("//"):
        link = "https:" + link

    # Заменяем короткие алиасы Bandcamp на полные путевые имена
    link = link.replace(".bandcamp.com/a/", ".bandcamp.com/album/")
    link = link.replace(".bandcamp.com/t/", ".bandcamp.com/track/")

    # 3. Исполнитель (artist)
    artist = (
        item.get("band_name")
        or item.get("artist")
        or item.get("artist_name")
        or item.get("secondary_text")
        or "Неизвестный исполнитель"
    )
    if isinstance(artist, str) and artist.startswith("by "):
        artist = artist[3:]

    # 4. Название релиза (title)
    title = (
        item.get("title")
        or item.get("album_title")
        or item.get("primary_text")
        or f"{GENRE.capitalize()} Release"
    )

    title_full = f"{title} by {artist}"

    # 5. Обложка (art_id)
    art_id = item.get("art_id") or item.get("primary_art_id") or item.get("image_id")
    image_url = f"https://f4.bcbits.com/img/a{art_id}_10.jpg" if art_id else ""

    genre = item.get("genre_text", GENRE)
    tags = [GENRE]
    if genre and genre.lower() != GENRE.lower():
        tags.append(genre.lower())

    return {
        "title_full": title_full,
        "artist": artist,
        "album_title": title,
        "image": image_url,
        "description": f"New {GENRE} release from {artist}.",
        "tags": tags,
        "link": link
    }

# ==============================================================================
# ОТПРАВКА В TELEGRAM
# ==============================================================================
def send_to_telegram(release):
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"

    title = html.escape(release["title_full"])
    desc = html.escape(release["description"])

    tags_list = [f"#{t.lower().replace('-', '_').replace(' ', '_')}" for t in release["tags"]]
    main_hashtag = f"#{GENRE.lower().replace('-', '_').replace(' ', '_')}"

    if main_hashtag not in tags_list:
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

    print(f"🎯 Выбранный жанр: {GENRE}")

    raw_items = fetch_from_discover_api()
    if not raw_items:
        raw_items = fetch_from_hub_api()

    if not raw_items:
        print("❌ Не удалось получить список релизов через API Bandcamp.")
        return

    releases = []
    for item in raw_items:
        details = parse_item_details(item)
        if details and details["link"]:
            releases.append(details)

    print(f"🔎 Успешно распознано релизов из API: {len(releases)}")

    already_posted = [r for r in releases if r["link"] in posted]
    new_releases = [r for r in releases if r["link"] not in posted]

    print(f"📊 Ранее опубликовано: {len(already_posted)}")
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
