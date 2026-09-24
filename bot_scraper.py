import os
import re
import json
import html
import requests
from bs4 import BeautifulSoup

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@bc_ambient")
SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY")
CLOUDFLARE_WORKER_URL = os.getenv("CLOUDFLARE_WORKER_URL", "")

POSTED_FILE = "posted_releases.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
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
    """Получает HTML страницы с использованием ScraperAPI, Cloudflare Worker или напрямую."""
    if SCRAPERAPI_KEY:
        req_url = f"http://api.scraperapi.com?api_key={SCRAPERAPI_KEY}&url={target_url}"
    elif CLOUDFLARE_WORKER_URL:
        req_url = CLOUDFLARE_WORKER_URL
    else:
        req_url = target_url

    try:
        res = requests.get(req_url, headers=HEADERS, timeout=30)
        return res
    except Exception as e:
        print(f"Ошибка сетевого запроса: {e}")
        return None

def extract_urls_from_json(obj):
    """Рекурсивно ищет ссылки на альбомы и треки внутри JSON-структуры Bandcamp."""
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
    
    res = fetch_html(target_url)
    if not res:
        return []

    print(f"[Bandcamp Page] Статус: {res.status_code}, Размер: {len(res.text)} байт")

    if res.status_code == 200 and len(res.text) > 10000:
        soup = BeautifulSoup(res.text, "html.parser")

        # Способ 1: Парсинг дата-блоба JSON
        pagedata = soup.find(id="pagedata") or soup.find(attrs={"data-blob": True})
        if pagedata and pagedata.get("data-blob"):
            try:
                blob_data = json.loads(pagedata["data-blob"])
                extracted_json_urls = extract_urls_from_json(blob_data)
                for url in extracted_json_urls:
                    clean_url = url.split("?")[0]
                    if not clean_url.startswith("https://bandcamp.com/"):
                        releases.append(clean_url)
                print(f"Извлечено из JSON data-blob: {len(releases)} релизов")
            except Exception as e:
                print(f"Ошибка разбора JSON data-blob: {e}")

        # Способ 2: Запасной поиск через исправленное регулярное выражение
        clean_text = res.text.replace(r'\/', '/').replace(r'\\/', '/')
        pattern = r'https://[a-zA-Z0-9\.-]+\.bandcamp\.com/(?:album|track)/[a-zA-Z0-9%_\.-]+'
        found_urls = re.findall(pattern, clean_text)

        for link in found_urls:
            clean_link = link.split("?")[0]
            if not clean_link.startswith("https://bandcamp.com/"):
                releases.append(clean_link)

    unique_releases = list(dict.fromkeys(releases))
    print(f"Итого найдено уникальных релизов: {len(unique_releases)}")
    return unique_releases

def fetch_release_details(url):
    """Извлекает обложку, название и описание конкретного альбома."""
    res = fetch_html(url)
    if not res or res.status_code != 200:
        return None

    soup = BeautifulSoup(res.text, "html.parser")

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
