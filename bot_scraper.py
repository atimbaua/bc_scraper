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
    """Универсально запрашивает любую страницу через ScraperAPI, Worker или напрямую."""
    if SCRAPERAPI_KEY:
        req_url = f"http://api.scraperapi.com?api_key={SCRAPERAPI_KEY}&url={target_url}"
    elif CLOUDFLARE_WORKER_URL:
        sep = "&" if "?" in CLOUDFLARE_WORKER_URL else "?"
        req_url = f"{CLOUDFLARE_WORKER_URL}{sep}url={target_url}"
    else:
        req_url = target_url

    try:
        res = requests.get(req_url, headers=HEADERS, timeout=30)
        return res
    except Exception as e:
        print(f"Ошибка сетевого запроса: {e}")
        return None

def clean_and_unescape(text):
    """Очищает текст от всех типов экранирования в Bandcamp (JSON/Unicode)."""
    text = text.replace(r'\/', '/').replace(r'\\/', '/')
    text = text.replace(r'\u002f', '/').replace(r'\u002F', '/')
    text = text.replace('&amp;', '&')
    return text

def get_new_ambient_releases():
    """Собирает ссылки на новые Ambient релизы с Bandcamp."""
    releases = []
    target_url = "https://bandcamp.com/tag/ambient?sort_field=date"
    
    res = fetch_html(target_url)
    if not res:
        return []

    print(f"[Bandcamp Page] Статус: {res.status_code}, Размер: {len(res.text)} байт")

    if res.status_code == 200 and len(res.text) > 5000:
        clean_text = clean_and_unescape(res.text)

        # Вытаскиваем абсолютно все URL-подобные строки из кода страницы
        raw_urls = re.findall(r'https?://[^\s"<>\'\\]+', clean_text)

        for url in raw_urls:
            url = url.rstrip('.,;)"\'')
            # Фильтруем только ссылки на альбомы и треки
            if '/album/' in url or '/track/' in url:
                # Очищаем от параметров запроса (?from=...)
                clean_url = url.split('?')[0].split('#')[0]
                # Исключаем служебные страницы самого bandcamp.com
                if not clean_url.startswith('https://bandcamp.com/'):
                    releases.append(clean_url)

    unique_releases = list(dict.fromkeys(releases))
    print(f"Итого найдено релизов: {len(unique_releases)}")

    # Диагностика на случай, если ничего не найдено
    if not unique_releases and len(res.text) > 0:
        print("[DEBUG] Диагностика страницы:")
        print(f"Содержит '/album/': {'/album/' in res.text}")
        print(f"Содержит '/track/': {'/track/' in res.text}")

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
        print(" Ошибка: Не задан TELEGRAM_BOT_TOKEN")
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
