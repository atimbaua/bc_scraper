import re
import os
import json
import html
import requests
from bs4 import BeautifulSoup

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@bc_ambient")
POSTED_FILE = "posted_releases.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
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

def get_new_ambient_releases():
    """Собирает ссылки на новые Ambient релизы с Bandcamp."""
    url = "https://bandcamp.com/tag/ambient?sort_field=date"
    response = requests.get(url, headers=HEADERS, timeout=15)
    
    if response.status_code != 200:
        print(f"Ошибка запроса Bandcamp: {response.status_code}")
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    releases = []

    # Способ 1: Парсинг встроенного JSON (data-blob)
    pagedata = soup.find(id="pagedata")
    if pagedata and pagedata.get("data-blob"):
        try:
            blob_data = json.loads(pagedata["data-blob"])
            # Извлекаем элементы из дисковера Bandcamp
            items = (
                blob_data.get("hub_data", {}).get("dig_deeper", {}).get("items", []) or
                blob_data.get("tab_data", {}).get("items", [])
            )
            for item in items:
                link = item.get("tralbum_url") or item.get("item_url")
                if link:
                    releases.append(link.split("?")[0])
        except Exception as e:
            print(f"Ошибка разбора data-blob: {e}")

    # Способ 2 (Резервный): Сканирование сырого текста страницы с помощью регулярных выражений
    if not releases:
        print("Поиск через JSON не дал результатов, задействуем регулярное выражение...")
        pattern = r'https?://[a-zA-Z0-9-]+\.bandcamp\.com/(?:album|track)/[a-zA-Z0-9_-]+'
        found_urls = re.findall(pattern, response.text)
        for link in found_urls:
            clean_link = link.split("?")[0]
            releases.append(clean_link)

    # Удаление дубликатов с сохранением порядка
    unique_releases = list(dict.fromkeys(releases))
    print(f"Найдено релизов на Bandcamp: {len(unique_releases)}")
    return unique_releases

def fetch_release_details(url):
    """Извлекает детальные метаданные (обложку, описание, исполнителя) со страницы релиза."""
    try:
        res = requests.get(url, headers=HEADERS, timeout=15)
        if res.status_code != 200:
            return None

        soup = BeautifulSoup(res.text, "html.parser")

        # Парсинг OpenGraph / Meta-тегов
        og_title = soup.find("meta", property="og:title")
        og_image = soup.find("meta", property="og:image")
        og_desc = soup.find("meta", property="og:description")

        title_full = og_title["content"] if og_title else "Ambient Release"
        image_url = og_image["content"] if og_image else ""
        description = og_desc["content"] if og_desc else ""

        # Извлечение тегов релиза
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
    """Форматирует сообщение и отправляет его в Telegram-канал."""
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"

    # Экранирование HTML-символов во избежание ошибок разметки
    title = html.escape(release["title_full"])
    desc = html.escape(release["description"][:300])
    if len(release["description"]) > 300:
        desc += "..."

    # Формирование хэштегов
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
    # Обрабатываем в обратном порядке (от более старых к самым новым)
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
