import os
import re
import json
import html
import requests
from bs4 import BeautifulSoup

# Попытка импорта curl_cffi (если установлен)
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

def fetch_html(target_url):
    """Получает HTML страницы с использованием ScraperAPI, curl_cffi или Cloudflare Worker."""
    # 1. ScraperAPI (если задан)
    if SCRAPERAPI_KEY:
        print(" [СПОСОБ ЗАПРОСА]: Используется ScraperAPI")
        req_url = f"http://api.scraperapi.com?api_key={SCRAPERAPI_KEY}&url={target_url}"
        try:
            res = requests.get(req_url, headers=HEADERS, timeout=30)
            if res.status_code == 200:
                return res.text
        except Exception as e:
            print(f"Ошибка ScraperAPI: {e}")

    # 2. curl_cffi (если установлен)
    if CURL_CFFI_AVAILABLE:
        print(" [СПОСОБ ЗАПРОСА]: Используется curl_cffi (Chrome 120)")
        try:
            res = curl_requests.get(target_url, headers=HEADERS, impersonate="chrome120", timeout=30)
            if res.status_code == 200:
                return res.text
        except Exception as e:
            print(f"Ошибка curl_cffi: {e}")

    # 3. Cloudflare Worker (если задан)
    if CLOUDFLARE_WORKER_URL:
        print(" [СПОСОБ ЗАПРОСА]: Используется Cloudflare Worker")
        sep = "&" if "?" in CLOUDFLARE_WORKER_URL else "?"
        req_url = f"{CLOUDFLARE_WORKER_URL}{sep}url={target_url}"
        try:
            res = requests.get(req_url, headers=HEADERS, timeout=30)
            if res.status_code == 200:
                return res.text
        except Exception as e:
            print(f"Ошибка Worker: {e}")

    # 4. Прямой запрос (резервный)
    print("⚠️ [ВНИМАНИЕ]: Прямой запрос без прокси")
    try:
        res = requests.get(target_url, headers=HEADERS, timeout=30)
        return res.text if res.status_code == 200 else None
    except Exception as e:
        print(f"Ошибка прямого запроса: {e}")
        return None

def extract_urls_from_json(obj):
    """Рекурсивно извлекает ссылки на релизы из JSON структуры Bandcamp."""
    urls = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in ("item_url", "tralbum_url", "page_url", "url") and isinstance(value, str):
                if "/album/" in value or "/track/" in value:
                    urls.append(value)
            else:
                urls.extend(extract_urls_from_json(value))
    elif isinstance(obj, list):
        for item in obj:
            urls.extend(extract_urls_from_json(item))
    return urls

def get_new_ambient_releases():
    """Собирает ссылки на новые Ambient релизы с Bandcamp."""
    releases = []
    target_url = "https://bandcamp.com/tag/ambient?sort_field=date"
    
    html_text = fetch_html(target_url)
    if not html_text:
        print("❌ Не удалось загрузить страницу Bandcamp.")
        return []

    print(f"[Bandcamp Page] Успешно загружено: {len(html_text)} байт")

    soup = BeautifulSoup(html_text, "html.parser")

    # Метод 1: Извлечение из data-blob (JSON контейнер Bandcamp)
    pagedata = soup.find(attrs={"data-blob": True})
    if pagedata and pagedata.get("data-blob"):
        try:
            blob_data = json.loads(pagedata["data-blob"])
            json_urls = extract_urls_from_json(blob_data)
            releases.extend(json_urls)
            print(f"Извлечено из JSON data-blob: {len(json_urls)} ссылок")
        except Exception as e:
            print(f"Ошибка разбора data-blob: {e}")

    # Метод 2: Резервный поиск Regex по всему тексту страницы
    clean_text = html.unescape(html_text).replace(r'\/', '/').replace(r'\\/', '/')
    pattern = r'https?://[a-zA-Z0-9.-]+\.bandcamp\.com/(?:album|track)/[^\s"\'<>\\?#]+'
    found_urls = re.findall(pattern, clean_text)
    releases.extend(found_urls)

    # Фильтрация и очистка ссылок
    cleaned_releases = []
    for url in releases:
        clean_url = url.split("?")[0].split("#")[0].rstrip('.,;)"\'')
        if not clean_url.startswith("https://bandcamp.com/") and not clean_url.startswith("http://bandcamp.com/"):
            cleaned_releases.append(clean_url)

    unique_releases = list(dict.fromkeys(cleaned_releases))
    print(f"Итого найдено уникальных релизов: {len(unique_releases)}")
    return unique_releases

def fetch_release_details(url):
    """Извлекает обложку, название и описание конкретного альбома."""
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
    """Отправляет готовый пост в Telegram-канал."""
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
        if link in posted:
            continue

        details = fetch_release_details(link)
        if not details or not details["image"]:
            continue

        print(f"Публикация: {details['title_full']}")
        if send_to_telegram(details):
            posted.add(link)
            new_posts += 1
        else:
            print(f"Не удалось отправить в Telegram: {link}")

    save_posted(posted)
    print(f"Завершено. Опубликовано новых релизов: {new_posts}")

if __name__ == "__main__":
    main()
