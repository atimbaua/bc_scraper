import os
import re
import json
import html
import requests
import cloudscraper
from bs4 import BeautifulSoup

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@bc_ambient")
POSTED_FILE = "posted_releases.json"

# Создаем скрейпер для обхода защиты Cloudflare
scraper = cloudscraper.create_scraper(
    browser={
        'browser': 'chrome',
        'platform': 'windows',
        'desktop': True
    }
)

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

def get_new_ambient_releases():
    """Собирает ссылки на новые Ambient релизы с Bandcamp."""
    releases = []

    # --- Способ 1: Парсинг страницы тега через Cloudscraper ---
    url = "https://bandcamp.com/tag/ambient?sort_field=date"
    try:
        res = scraper.get(url, timeout=20)
        print(f"[Bandcamp Tag] Статус: {res.status_code}, Размер ответа: {len(res.text)} байт")

        if res.status_code == 200 and len(res.text) > 10000:
            # Разэкранируем слэши (https:\/\/... -> https://...)
            clean_text = res.text.replace(r'\/', '/')

            # Ищем все ссылки на альбомы и треки Bandcamp
            pattern = r'https://[a-zA-Z0-9\.-]+\.bandcamp\.com/(?:album|track)/[a-zA-Z0-9%_-]+'
            found_urls = re.findall(pattern, clean_text)

            for link in found_urls:
                clean_link = link.split("?")[0]
                releases.append(clean_link)
    except Exception as e:
        print(f"Ошибка при получении страницы Bandcamp: {e}")

    # --- Способ 2: Запасной запрос к внутреннему API через Cloudscraper ---
    if not releases:
        print("Сканирование страницы не дало результатов, пробуем API...")
        api_url = "https://bandcamp.com/api/hub/2/dig_deeper"
        payload = {"tag": "ambient", "page": 0, "sort": "date"}
        try:
            res = scraper.post(api_url, json=payload, timeout=20)
            print(f"[Bandcamp API] Статус: {res.status_code}, Размер: {len(res.text)} байт")
            if res.status_code == 200:
                data = res.json()
                items = data.get("items", []) or data.get("results", [])
                for item in items:
                    link = item.get("tralbum_url") or item.get("item_url")
                    if link:
                        releases.append(link.split("?")[0])
        except Exception as e:
            print(f"Ошибка API Bandcamp: {e}")

    unique_releases = list(dict.fromkeys(releases))
    print(f"Итого найдено релизов: {len(unique_releases)}")
    return unique_releases

def fetch_release_details(url):
    """Извлекает обложку, название и описание релиза."""
    try:
        res = scraper.get(url, timeout=20)
        if res.status_code != 200:
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
    except Exception as e:
        print(f"Ошибка при обработке релиза {url}: {e}")
        return None

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
    # Публикуем от более старых к новым
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
