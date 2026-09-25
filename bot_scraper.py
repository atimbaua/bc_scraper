import os
import re
import json
import html
import time
import csv
from datetime import datetime
import requests
from bs4 import BeautifulSoup

# Попытка импорта curl_cffi для обхода Cloudflare
try:
    from curl_cffi import requests as curl_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@bc_ambient")
SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY")
CLOUDFLARE_WORKER_URL = os.getenv("CLOUDFLARE_WORKER_URL")

POSTED_FILE = "posted_releases.json"
CSV_FILE = "releases_data.csv"
MAX_POSTS_PER_RUN = 3

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

def load_posted():
    """Загружает список уже опубликованных релизов."""
    if os.path.exists(POSTED_FILE):
        try:
            with open(POSTED_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()

def save_posted(posted_set):
    """Сохраняет обновленный список опубликованных релизов."""
    with open(POSTED_FILE, "w", encoding="utf-8") as f:
        json.dump(list(posted_set), f, ensure_ascii=False, indent=2)

def save_to_csv(details):
    """Добавляет информацию о релизе строкой в CSV-файл."""
    file_exists = os.path.exists(CSV_FILE)
    
    full_title = details.get("title_full", "")
    # Заголовок Bandcamp обычно выглядит как "Album Name by Artist Name"
    if " by " in full_title:
        album_title, artist = full_title.rsplit(" by ", 1)
    else:
        album_title = full_title
        artist = "Неизвестен"

    published_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    tags_str = ", ".join(details.get("tags", []))

    with open(CSV_FILE, mode="a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        # Если файл создается впервые — записываем заголовки колонок
        if not file_exists:
            writer.writerow(["published_at_utc", "artist", "album_title", "url", "tags", "image_url"])
        
        writer.writerow([
            published_at,
            artist.strip(),
            album_title.strip(),
            details.get("link", ""),
            tags_str,
            details.get("image", "")
        ])

def is_valid_bandcamp_page(text):
    if not text:
        return False
    return len(text) > 10000 and ("data-blob" in text or "bandcamp" in text.lower())

def fetch_html(target_url):
    if CURL_CFFI_AVAILABLE:
        try:
            res = curl_requests.get(target_url, headers=HEADERS, impersonate="chrome120", timeout=30)
            if res.status_code == 200 and is_valid_bandcamp_page(res.text):
                return res.text
        except Exception as e:
            print(f" Ошибка curl_cffi: {e}")

    if SCRAPERAPI_KEY:
        req_url = f"http://api.scraperapi.com?api_key={SCRAPERAPI_KEY}&url={target_url}&render=true"
        try:
            res = requests.get(req_url, headers=HEADERS, timeout=60)
            if res.status_code == 200 and is_valid_bandcamp_page(res.text):
                return res.text
        except Exception as e:
            print(f" Ошибка ScraperAPI: {e}")

    if CLOUDFLARE_WORKER_URL:
        sep = "&" if "?" in CLOUDFLARE_WORKER_URL else "?"
        req_url = f"{CLOUDFLARE_WORKER_URL}{sep}url={target_url}"
        try:
            res = requests.get(req_url, headers=HEADERS, timeout=30)
            if res.status_code == 200 and is_valid_bandcamp_page(res.text):
                return res.text
        except Exception as e:
            print(f" Ошибка Worker: {e}")

    return None

def extract_urls_from_json(obj):
    urls = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in ("item_url", "tralbum_url", "page_url", "url", "band_url") and isinstance(value, str):
                if "/album/" in value or "/track/" in value:
                    urls.append(value)
            else:
                urls.extend(extract_urls_from_json(value))
    elif isinstance(obj, list):
        for item in obj:
            urls.extend(extract_urls_from_json(item))
    return urls

def get_new_ambient_releases():
    releases = []
    target_url = "https://bandcamp.com/tag/ambient?sort_field=date"
    
    html_text = fetch_html(target_url)
    if not html_text:
        print(" Не удалось загрузить страницу Bandcamp.")
        return []

    soup = BeautifulSoup(html_text, "html.parser")

    pagedata = soup.find(attrs={"data-blob": True})
    if pagedata and pagedata.get("data-blob"):
        try:
            blob_data = json.loads(pagedata["data-blob"])
            json_urls = extract_urls_from_json(blob_data)
            releases.extend(json_urls)
        except Exception as e:
            print(f"Ошибка разбора data-blob: {e}")

    clean_text = html.unescape(html_text).replace(r'\/', '/').replace(r'\\/', '/')
    pattern = r'https?://[a-zA-Z0-9.-]+\.bandcamp\.com/(?:album|track)/[^\s"\'<>\\?#]+'
    found_urls = re.findall(pattern, clean_text)
    releases.extend(found_urls)

    cleaned_releases = []
    for url in releases:
        clean_url = url.split("?")[0].split("#")[0].rstrip('.,;)"\'')
        if not clean_url.startswith("https://bandcamp.com/") and not clean_url.startswith("http://bandcamp.com/"):
            cleaned_releases.append(clean_url)

    return list(dict.fromkeys(cleaned_releases))

def fetch_release_details(url):
    html_text = fetch_html(url)
    if not html_text:
        return None

    soup = BeautifulSoup(html_text, "html.parser")

    og_title = soup.find("meta", property="og:title")
    og_image = soup.find("meta", property="og:image")
    og_desc = soup.find("meta", property="og:description")

    title_full = og_title["content"] if og_title else "Ambient Release"
    image_url = og_image["content"] if og_image else ""
    description = og_desc["content"] if og_desc else ""

    tags = [t.get_text(strip=True) for t in soup.select("a.tag")]

    return {
        "title_full": title_full,
        "image": image_url,
        "description": description,
        "tags": tags[:6],
        "link": url
    }

def send_to_telegram(release):
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"

    title = html.escape(release["title_full"])
    desc = html.escape(release["description"][:300])
    if len(release["description"]) > 300:
        desc += "..."

    tags_list = [f"#{t.lower().replace('-', '_').replace(' ', '_')}" for t in release["tags"]]
    if "#ambient" not in tags_list:
        tags_list.insert(0, "#ambient")
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

def main():
    if not TELEGRAM_BOT_TOKEN:
        print("Ошибка: Не задан TELEGRAM_BOT_TOKEN")
        return

    posted = load_posted()
    release_links = get_new_ambient_releases()

    new_posts = 0
    for link in reversed(release_links):
        if new_posts >= MAX_POSTS_PER_RUN:
            print(f"Достигнут лимит в {MAX_POSTS_PER_RUN} постов за запуск. Остановка.")
            break

        if link in posted:
            continue

        details = fetch_release_details(link)
        if not details or not details["image"]:
            continue

        print(f"Публикация: {details['title_full']}")
        if send_to_telegram(details):
            posted.add(link)
            save_to_csv(details)  # <--- СОХРАНЕНИЕ В CSV
            new_posts += 1
            time.sleep(3)
        else:
            print(f"Не удалось отправить в Telegram: {link}")

    save_posted(posted)
    print(f"Завершено. Опубликовано новых релизов: {new_posts}")

if __name__ == "__main__":
    main()
