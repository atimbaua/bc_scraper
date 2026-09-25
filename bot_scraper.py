import os
import re
import json
import html
import time
import csv
from datetime import datetime
import requests
from bs4 import BeautifulSoup

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

def init_csv_file():
    """Гарантирует существование CSV-файла с заголовками при запуске."""
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, mode="w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["published_at_utc", "artist", "album_title", "url", "tags", "image_url"])

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
    full_title = details.get("title_full", "")
    if " by " in full_title:
        album_title, artist = full_title.rsplit(" by ", 1)
    else:
        album_title = full_title
        artist = "Неизвестен"

    published_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    tags_str = ", ".join(details.get("tags", []))

    with open(CSV_FILE, mode="a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
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
    """
    Каскадный запрос с подробным логом каждой попытки.
    """
    # 1. Попытка через curl_cffi
    if CURL_CFFI_AVAILABLE:
        print(" [СПОСОБ 1]: Пробуем curl_cffi...")
        try:
            res = curl_requests.get(target_url, headers=HEADERS, impersonate="chrome120", timeout=30)
            if res.status_code == 200 and is_valid_bandcamp_page(res.text):
                print(f"    Успех curl_cffi! Размер: {len(res.text)} байт")
                return res.text
            print(f"   ⚠️ curl_cffi: Код {res.status_code}, размер {len(res.text) if res.text else 0} байт")
        except Exception as e:
            print(f"   ⚠️ Ошибка curl_cffi: {e}")

    # 2. Попытка через ScraperAPI (обычный запрос без render=true для экономии лимитов)
    if SCRAPERAPI_KEY:
        print(" [СПОСОБ 2]: Пробуем ScraperAPI...")
        req_url = f"http://api.scraperapi.com?api_key={SCRAPERAPI_KEY}&url={target_url}"
        try:
            res = requests.get(req_url, headers=HEADERS, timeout=40)
            if res.status_code == 200 and is_valid_bandcamp_page(res.text):
                print(f"    Успех ScraperAPI! Размер: {len(res.text)} байт")
                return res.text
            print(f"   ⚠️ ScraperAPI: Код {res.status_code}, размер {len(res.text) if res.text else 0} байт")
        except Exception as e:
            print(f"   ⚠️ Ошибка ScraperAPI: {e}")

    # 3. Попытка прямой запрос (Fallback)
    print(" [СПОСОБ 3]: Прямой запрос requests...")
    try:
        res = requests.get(target_url, headers=HEADERS, timeout=20)
        if res.status_code == 200 and is_valid_bandcamp_page(res.text):
            print(f"    Успех прямой запрос! Размер: {len(res.text)} байт")
            return res.text
        print(f"   ⚠️ Прямой запрос: Код {res.status_code}, размер {len(res.text) if res.text else 0} байт")
    except Exception as e:
        print(f"   ⚠️ Ошибка прямого запроса: {e}")

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
    target_url = "https://bandcamp.com/tag/ambient?sort_field=date"
    html_text = fetch_html(target_url)
    if not html_text:
        print("❌ Не удалось загрузить страницу Bandcamp ни одним из способов.")
        return []

    soup = BeautifulSoup(html_text, "html.parser")
    releases = []

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

    unique_releases = list(dict.fromkeys(cleaned_releases))
    print(f"🔎 Найдено уникальных релизов на странице Bandcamp: {len(unique_releases)}")
    return unique_releases

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

    init_csv_file()

    posted = load_posted()
    release_links = get_new_ambient_releases()

    if not release_links:
        print("ℹ️ Ссылок на релизы не обнаружено.")
        return

    # Считаем, сколько из найденных релизов уже было в базе
    already_posted_count = sum(1 for link in release_links if link in posted)
    new_to_process = [link for link in release_links if link not in posted]

    print(f"📊 Уже опубликовано ранее: {already_posted_count}")
    print(f"✨ Новых релизов для публикации: {len(new_to_process)}")

    new_posts = 0
    for link in reversed(new_to_process):
        if new_posts >= MAX_POSTS_PER_RUN:
            print(f"Достигнут лимит в {MAX_POSTS_PER_RUN} постов за запуск. Остановка.")
            break

        details = fetch_release_details(link)
        if not details or not details["image"]:
            continue

        print(f"🚀 Публикация: {details['title_full']}")
        if send_to_telegram(details):
            posted.add(link)
            save_to_csv(details)
            new_posts += 1
            time.sleep(3)
        else:
            print(f"❌ Не удалось отправить в Telegram: {link}")

    save_posted(posted)
    print(f"🏁 Завершено. Опубликовано новых релизов: {new_posts}")
    print(f"Завершено. Опубликовано новых релизов: {new_posts}")

if __name__ == "__main__":
    main()
