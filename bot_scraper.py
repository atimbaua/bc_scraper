import os
import json
import html
import time
import csv
import re
import urllib.parse
from datetime import datetime, timezone
import requests

try:
    from curl_cffi import requests as curl_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False

# ==============================================================================
# НАСТРОЙКИ
# ==============================================================================
GENRES = [
    # "ambient"
]

TARGET_ARTISTS_AND_LABELS = [
    "https://windyandcarl.bandcamp.com"
]

MAX_DAYS_AGO = 30        # Публиковать релизы не старше N дней
ALLOW_UPCOMING = True    # Публиковать предзаказы / анонсы

MAX_POSTS_PER_RUN = 5
POSTED_FILE = "posted_releases.json"
CSV_FILE = "releases_data.csv"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@bc_ambient")
SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY")

def get_headers():
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

# ==============================================================================
# РАБОТА С ФАЙЛАМИ
# ==============================================================================
def init_csv_file():
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
    published_at_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
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
# ПАРСИНГ ДАТЫ СО СТРАНИЦЫ РЕЛИЗА
# ==============================================================================
def parse_date_from_html(html_text):
    if not html_text:
        return None

    # 1. Поиск "released Month DD, YYYY" или "releases Month DD, YYYY"
    date_match = re.search(r'(?:released|releases)\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})', html_text, re.IGNORECASE)
    if date_match:
        date_str = date_match.group(1).strip()
        for fmt in ("%B %d, %Y", "%b %d, %Y"):
            try:
                return datetime.strptime(date_str, fmt).date()
            except ValueError:
                pass

    # 2. Поиск "released DD Month YYYY" / "releases DD Month YYYY"
    date_match_alt = re.search(r'(?:released|releases)\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})', html_text, re.IGNORECASE)
    if date_match_alt:
        date_str = date_match_alt.group(1).strip()
        for fmt in ("%d %B %Y", "%d %b %Y"):
            try:
                return datetime.strptime(date_str, fmt).date()
            except ValueError:
                pass

    # 3. Поиск itemprop="datePublished" content="YYYYMMDD" или "YYYY-MM-DD"
    meta_dp = re.search(r'itemprop="datePublished"\s+content="(\d{8}|\d{4}-\d{2}-\d{2})"', html_text, re.IGNORECASE)
    if meta_dp:
        d_str = meta_dp.group(1).strip()
        if len(d_str) == 8:
            try:
                return datetime.strptime(d_str, "%Y%m%d").date()
            except ValueError:
                pass
        else:
            try:
                return datetime.strptime(d_str, "%Y-%m-%d").date()
            except ValueError:
                pass

    # 4. Поиск datePublished / releaseDate в JSON-LD
    json_ld = re.search(r'"(?:datePublished|releaseDate)"\s*:\s*"([^"]+)"', html_text, re.IGNORECASE)
    if json_ld:
        raw_d = json_ld.group(1).strip()
        try:
            if "T" in raw_d:
                return datetime.fromisoformat(raw_d.replace("Z", "+00:00")).date()
            return datetime.strptime(raw_d[:10], "%Y-%m-%d").date()
        except Exception:
            pass

    return None

def fetch_release_date(url):
    headers = get_headers()
    html_text = ""

    # 1. ScraperAPI
    if SCRAPERAPI_KEY:
        try:
            clean_key = SCRAPERAPI_KEY.strip().strip('"').strip("'")
            encoded_url = urllib.parse.quote(url, safe='')
            scraper_url = f"http://api.scraperapi.com?api_key={clean_key}&url={encoded_url}"
            res = requests.get(scraper_url, timeout=30)
            if res.status_code == 200:
                html_text = res.text
        except Exception as e:
            print(f"   ⚠️ Ошибка fetch_release_date (ScraperAPI): {e}")

    # 2. curl_cffi
    if not html_text and CURL_CFFI_AVAILABLE:
        try:
            res = curl_requests.get(url, headers=headers, impersonate="chrome120", timeout=30)
            if res.status_code == 200:
                html_text = res.text
        except Exception as e:
            print(f"   ⚠️ Ошибка fetch_release_date (curl_cffi): {e}")

    # 3. Прямой requests
    if not html_text:
        try:
            res = requests.get(url, headers=headers, timeout=20)
            if res.status_code == 200:
                html_text = res.text
        except Exception as e:
            print(f"   ⚠️ Ошибка fetch_release_date (requests): {e}")

    return parse_date_from_html(html_text)

def is_release_date_valid(release_date):
    if release_date is None:
        print("   ⚠️ Дата релиза не найдена на странице, пропуск для защиты от спама.")
        return False

    today = datetime.now(timezone.utc).date()
    days_diff = (today - release_date).days

    if days_diff < 0:
        is_ok = ALLOW_UPCOMING
        status = "Анонс / Предзаказ" if is_ok else "Пропущен (предзаказ)"
        print(f"   📅 Дата релиза: {release_date} ({status})")
        return is_ok
    else:
        is_ok = days_diff <= MAX_DAYS_AGO
        status = f"{days_diff} дн. назад" if is_ok else f"Устарел ({days_diff} дн. назад)"
        print(f"   📅 Дата релиза: {release_date} ({status})")
        return is_ok

# ==============================================================================
# ИЗВЛЕЧЕНИЕ TRALBUM_ID СО СТРАНИЦЫ
# ==============================================================================
def fetch_tralbum_id_fallback(url):
    """
    Резервный забор tralbum_id напрямую со страницы релиза.
    """
    headers = get_headers()
    html_text = ""
    if CURL_CFFI_AVAILABLE:
        try:
            res = curl_requests.get(url, headers=headers, impersonate="chrome120", timeout=15)
            if res.status_code == 200:
                html_text = res.text
        except Exception:
            pass

    if not html_text:
        try:
            res = requests.get(url, headers=headers, timeout=15)
            if res.status_code == 200:
                html_text = res.text
        except Exception:
            pass

    if html_text:
        m = re.search(r'var\s+TralbumData\s*=\s*\{[^}]*?\bid\s*:\s*(\d+)', html_text, re.IGNORECASE)
        if m:
            return int(m.group(1))
        m_attr = re.search(r'data-tralbum-id="(\d+)"', html_text)
        if m_attr:
            return int(m_attr.group(1))
    return 0

# ==============================================================================
# ПАРСИНГ АРТИСТОВ / ЛЕЙБЛОВ В СООТВЕТСТВИИ С TRALBUM_ID
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
    print(f" 👤 [АРТИСТ/ЛЕЙБЛ]: Проверяем каталог {subdomain} ({url})...")

    html_text = ""
    if CURL_CFFI_AVAILABLE:
        try:
            res = curl_requests.get(url, headers=headers, impersonate="chrome120", timeout=30)
            if res.status_code == 200:
                html_text = res.text
        except Exception as e:
            print(f"   ⚠️ Ошибка запроса к артисту {subdomain} (curl_cffi): {e}")

    if not html_text:
        try:
            res = requests.get(url, headers=headers, timeout=20)
            if res.status_code == 200:
                html_text = res.text
        except Exception as e:
            print(f"   ⚠️ Ошибка requests к артисту {subdomain}: {e}")

    if not html_text and SCRAPERAPI_KEY:
        try:
            clean_key = SCRAPERAPI_KEY.strip().strip('"').strip("'")
            encoded_url = urllib.parse.quote(url, safe='')
            scraper_url = f"http://api.scraperapi.com?api_key={clean_key}&url={encoded_url}"
            res = requests.get(scraper_url, timeout=30)
            if res.status_code == 200:
                html_text = res.text
        except Exception as e:
            print(f"   ⚠️ Ошибка ScraperAPI к артисту {subdomain}: {e}")

    if not html_text:
        print(f"   ⚠️ Не удалось получить страницу артиста {subdomain}")
        return []

    artist_match = re.search(r'<meta\s+property="og:site_name"\s+content="([^"]+)"', html_text, re.IGNORECASE)
    if not artist_match:
        artist_match = re.search(r'<title>([^<]+)</title>', html_text, re.IGNORECASE)
    artist_name = artist_match.group(1).split('|')[0].strip() if artist_match else subdomain

    raw_items = []
    seen_links = set()

    # 1. Извлекаем релизы из data-client-items
    client_items_match = re.search(r'data-client-items="([^"]+)"', html_text)
    if client_items_match:
        try:
            raw_json = html.unescape(client_items_match.group(1))
            client_items = json.loads(raw_json)
            for c_item in client_items:
                path = c_item.get("page_url") or c_item.get("title_link")
                title = c_item.get("title")
                art_id = c_item.get("art_id")
                
                # Достаем id / item_id / tralbum_id
                tralbum_id = c_item.get("id") or c_item.get("item_id") or c_item.get("tralbum_id") or 0

                if path and title:
                    full_link = f"https://{subdomain}.bandcamp.com{path}" if path.startswith("/") else path
                    full_link = full_link.replace(".bandcamp.com/a/", ".bandcamp.com/album/").replace(".bandcamp.com/t/", ".bandcamp.com/track/")
                    if full_link not in seen_links and ("/album/" in full_link or "/track/" in full_link):
                        seen_links.add(full_link)
                        img_url = f"https://f4.bcbits.com/img/a{art_id}_10.jpg" if art_id else ""
                        raw_items.append({
                            "tralbum_id": int(tralbum_id),
                            "title_full": f"{title} by {artist_name}",
                            "artist": artist_name,
                            "album_title": title,
                            "image": img_url,
                            "description": f"New release from {artist_name}.",
                            "tags": [subdomain],
                            "link": full_link,
                            "genre": "artist/label"
                        })
        except Exception as e:
            print(f"   ⚠️ Ошибка чтения data-client-items: {e}")

    # 2. Запасной доступ через ссылки HTML
    if not raw_items:
        all_album_links = re.findall(r'href="(/(?:album|track)/[a-zA-Z0-9\-_]+)"', html_text, re.IGNORECASE)
        for path in all_album_links:
            full_link = f"https://{subdomain}.bandcamp.com{path}"
            if full_link not in seen_links:
                seen_links.add(full_link)
                title_slug = path.split("/")[-1].replace("-", " ").title()
                raw_items.append({
                    "tralbum_id": 0,
                    "title_full": f"{title_slug} by {artist_name}",
                    "artist": artist_name,
                    "album_title": title_slug,
                    "image": "",
                    "description": f"New release from {artist_name}.",
                    "tags": [subdomain],
                    "link": full_link,
                    "genre": "artist/label"
                })

    # 3. Гарантируем наличие tralbum_id у каждого релиза
    for item in raw_items:
        if item["tralbum_id"] == 0:
            item["tralbum_id"] = fetch_tralbum_id_fallback(item["link"])

    # 4. СОБИРАЕМ ВСЕ tralbum_id В ОДИН МАССИВ И СОРТИРУЕМ ПО УБЫВАНИЮ
    tralbum_ids = [item["tralbum_id"] for item in raw_items if item["tralbum_id"] > 0]
    tralbum_ids.sort(reverse=True)

    print(f" 🔢 [TRALBUM_ID] Собран и отсортирован массив tralbum_id (по убыванию):")
    print(f"    {tralbum_ids}")

    # 5. СОРТИРУЕМ ВСЕ РЕЛИЗЫ НА ОСНОВЕ МАССИВА tralbum_id (ОТ НАИБОЛЬШЕГО К НАИМЕНЬШЕМУ)
    raw_items.sort(key=lambda x: x["tralbum_id"], reverse=True)

    print(f"    Найдено ВСЕГО релизов у {artist_name}: {len(raw_items)}")
    return raw_items

# ==============================================================================
# ОТПРАВКА В TELEGRAM
# ==============================================================================
def send_to_telegram(release):
    if not TELEGRAM_BOT_TOKEN:
        print(" Ошибка: TELEGRAM_BOT_TOKEN не задан")
        return False

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

    if release.get("image"):
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "photo": release["image"],
            "caption": caption,
            "parse_mode": "HTML"
        }
    else:
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": caption,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        }

    try:
        resp = requests.post(api_url, data=payload, timeout=20)
        return resp.ok
    except Exception as e:
        print(f"❌ Ошибка отправки в Telegram: {e}")
        return False

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

    # 1. Собираем релизы по артистам/лейблам (они уже отсортированы по tralbum_id descending)
    for target in TARGET_ARTISTS_AND_LABELS:
        artist_items = fetch_from_artist_or_label(target)
        for details in artist_items:
            if details and details["link"] and details["link"] not in seen_links:
                seen_links.add(details["link"])
                releases.append(details)

    print(f"\n🔎 Всего распознано уникальных релизов: {len(releases)}")

    new_releases = [r for r in releases if r["link"] not in posted]
    print(f"✨ Новых не опубликованных ранее релизов: {len(new_releases)}")

    new_posts = 0
    for release in new_releases:
        if new_posts >= MAX_POSTS_PER_RUN:
            print(f"🛑 Достигнут лимит в {MAX_POSTS_PER_RUN} постов за запуск. Остановка.")
            break

        print(f"\n🔍 Проверка даты для: {release['title_full']} (tralbum_id: {release.get('tralbum_id')})")
        
        release_date = fetch_release_date(release["link"])

        if not is_release_date_valid(release_date):
            continue

        print(f"🚀 Публикация в Telegram: {release['title_full']}")
        if send_to_telegram(release):
            posted.add(release["link"])
            save_to_csv(release)
            new_posts += 1
            time.sleep(3)
        else:
            print(f"❌ Не удалось отправить в Telegram: {release['link']}")

    save_posted(posted)
    print(f"\n🏁 Завершено. Опубликовано новых релизов: {new_posts}")

if __name__ == "__main__":
    main()
