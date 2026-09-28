import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests

# Попытка импорта curl_cffi для обхода защиты Bandcamp
try:
    from curl_cffi import requests as curl_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False

# ==========================================
# КОНФИГУРАЦИЯ И ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ
# ==========================================
SCRAPERAPI_KEY = os.environ.get("SCRAPERAPI_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Список жанров для проверки
GENRES = ["ambient", "downtempo", "chillout"]

# Файл с историей ранее опубликованных релизов
SENT_RELEASES_FILE = "sent_releases.json"


def get_headers():
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://bandcamp.com/discover",
    }


def load_sent_releases():
    if os.path.exists(SENT_RELEASES_FILE):
        try:
            with open(SENT_RELEASES_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception as e:
            print(f"⚠️ Ошибка чтения файла истории: {e}")
    return set()


def save_sent_releases(sent_set):
    try:
        with open(SENT_RELEASES_FILE, "w", encoding="utf-8") as f:
            json.dump(list(sent_set), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ Ошибка сохранения истории: {e}")


def fetch_from_discover_api(genre_name):
    """
    Получение релизов из Bandcamp Discover API с каскадом fallback-стратегий:
    1. Стандартный requests (прямой запрос)
    2. curl_cffi (эмуляция Chrome 120)
    3. ScraperAPI (резервный прокси с корректным URL-encoding)
    """
    clean_genre = urllib.parse.quote(genre_name.strip())
    target_url = f"https://bandcamp.com/api/discover/3/get_web?g={clean_genre}&s=date&p=0"
    headers = get_headers()

    # 1. Прямой запрос через обычный requests
    try:
        res = requests.get(target_url, headers=headers, timeout=15)
        if res.status_code == 200:
            data = res.json()
            items = data.get("items", [])
            if items:
                return items
    except Exception as e:
        print(f"   ⚠️ Ошибка Discover API (requests): {e}")

    # 2. Обход защиты через curl_cffi
    if CURL_CFFI_AVAILABLE:
        try:
            res = curl_requests.get(
                target_url,
                headers=headers,
                impersonate="chrome120",
                timeout=20
            )
            if res.status_code == 200:
                data = res.json()
                items = data.get("items", [])
                if items:
                    return items
        except Exception as e:
            print(f"   ⚠️ Ошибка Discover API (curl_cffi): {e}")

    # 3. Резервный канал через ScraperAPI
    if SCRAPERAPI_KEY:
        try:
            clean_key = SCRAPERAPI_KEY.strip().strip('"').strip("'")
            encoded_target = urllib.parse.quote(target_url, safe='')
            scraper_url = f"http://api.scraperapi.com?api_key={clean_key}&url={encoded_target}"
            
            res = requests.get(scraper_url, timeout=30)
            if res.status_code == 200:
                data = res.json()
                items = data.get("items", [])
                if items:
                    return items
        except Exception as e:
            print(f"   ⚠️ Ошибка Discover API (ScraperAPI): {e}")

    return []


def send_telegram_message(text, photo_url=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"📱 [Эмуляция Telegram]:\n{text}\n")
        return True

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    
    if photo_url:
        endpoint = f"{api_url}/sendPhoto"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "photo": photo_url,
            "caption": text,
            "parse_mode": "HTML"
        }
    else:
        endpoint = f"{api_url}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        }

    try:
        response = requests.post(endpoint, json=payload, timeout=15)
        return response.status_code == 200
    except Exception as e:
        print(f"⚠️ Ошибка отправки в Telegram: {e}")
        return False


def main():
    print("🚀 Запуск скрапера Bandcamp...")
    sent_releases = load_sent_releases()
    new_checked_count = 0
    published_count = 0

    now_utc = datetime.now(timezone.utc)
    cutoff_time = now_utc - timedelta(hours=24)

    for genre in GENRES:
        print(f"\n🎧 [{genre.upper()}]: Запрос к Discover API...")
        items = fetch_from_discover_api(genre)

        if not items:
            print(f"   ⚠️ Не удалось получить список релизов для {genre}.")
            continue

        for item in items:
            item_id = str(item.get("id") or item.get("item_id", ""))
            album_url = item.get("tralbum_url") or item.get("item_url")

            if not album_url or item_id in sent_releases:
                continue

            new_checked_count += 1
            
            # Извлечение даты релиза из ответа API (если доступна)
            pub_date_str = item.get("publish_date") or item.get("release_date")
            is_recent = True

            if pub_date_str:
                try:
                    # Пример парсинга формата "DD MMM YYYY HH:MM:SS GMT"
                    pub_date = datetime.strptime(pub_date_str, "%d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc)
                    if pub_date < cutoff_time:
                        is_recent = False
                except Exception:
                    pass

            if is_recent:
                artist = item.get("artist", "Неизвестный исполнитель")
                title = item.get("title", "Без названия")
                art_id = item.get("art_id")
                photo_url = f"https://f4.bcbits.com/img/a{art_id}_10.jpg" if art_id else None

                msg = (
                    f"🎧 <b>Новый релиз [{genre.capitalize()}]</b>\n\n"
                    f"<b>{artist}</b> — {title}\n\n"
                    f"🔗 <a href='{album_url}'>Слушать на Bandcamp</a>"
                )

                if send_telegram_message(msg, photo_url):
                    sent_releases.add(item_id)
                    published_count += 1
                    time.sleep(1)  # Пауза между сообщениями

    save_sent_releases(sent_releases)

    print(f"\n🔎 Новых (ранее не отправленных) релизов для проверки даты: {new_checked_count}")
    print(f"✨ Релизов, вышедших за последние 24 ч.: {published_count}")
    print(f"🏁 Завершено. Опубликовано новых релизов: {published_count}")


if __name__ == "__main__":
    main()
