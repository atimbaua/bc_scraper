import os
import json
import time
import csv
import random
from datetime import datetime, timezone, date

# ==============================================================================
# НАСТРОЙКА ТРАНСПОРТА (обход Cloudflare/Fastly)
# ==============================================================================
# ВАЖНО: переменная должна быть установлена ДО импорта py_bandcamp
os.environ["PYBANDCAMP_TRANSPORT"] = "curl_cffi"

from py_bandcamp import BandCamp

# ==============================================================================
# НАСТРОЙКИ
# ==============================================================================
GENRES = [
    # "ambient"
]

TARGET_ARTISTS_AND_LABELS = [
    "https://sessionvictim.bandcamp.com"
]

MAX_DAYS_AGO = 30
ALLOW_UPCOMING = True

MAX_POSTS_PER_RUN = 5
POSTED_FILE = "posted_releases.json"
CSV_FILE = "releases_data.csv"

DELAY_BETWEEN_RELEASES = (2.0, 4.0)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@bc_ambient")

# ==============================================================================
# ФАЙЛЫ
# ==============================================================================
def init_csv_file():
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, mode="w", encoding="utf-8", newline="") as f:
            csv.writer(f).writerow([
                "published_at_utc", "release_date", "genre", "artist",
                "album_title", "url", "tags", "image_url"
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
    rd = details.get("release_date")
    rd_str = rd.strftime("%Y-%m-%d") if rd else ""
    with open(CSV_FILE, mode="a", encoding="utf-8", newline="") as f:
        csv.writer(f).writerow([
            published_at_utc, rd_str,
            details.get("genre", "ambient"),
            details.get("artist", "Неизвестный артист").strip(),
            details.get("album_title", "Без названия").strip(),
            details.get("link", "").strip(),
            ", ".join(details.get("tags", [])),
            details.get("image", "").strip()
        ])


# ==============================================================================
# СБОР РЕЛИЗОВ ЧЕРЕЗ py_bandcamp
# ==============================================================================
def extract_subdomain(target):
    target = target.strip().lower()
    target = target.replace("https://", "").replace("http://", "")
    return target.split('.')[0].split('/')[0]


def fetch_releases_from_artist(target):
    """Собирает все релизы (альбомы и треки) артиста/лейбла через py_bandcamp."""
    subdomain = extract_subdomain(target)
    if not subdomain:
        return []

    print(f" 👤 [АРТИСТ/ЛЕЙБЛ]: {subdomain}...")

    releases = []
    seen_links = set()

    try:
        artist = BandCamp.artist_to_entity(f"https://{subdomain}.bandcamp.com")
        artist_name = artist.name or subdomain
        print(f"   Артист: {artist_name}")
    except Exception as e:
        print(f"   ⚠️ Не удалось получить артиста: {e}")
        artist_name = subdomain

    # Собираем релизы из каталога артиста
    urls_to_try = [
        f"https://{subdomain}.bandcamp.com/music",
        f"https://{subdomain}.bandcamp.com/albums",
    ]

    for url in urls_to_try:
        try:
            # py_bandcamp умеет парсить страницу артиста и возвращать список альбомов
            albums = BandCamp.artist_albums(f"https://{subdomain}.bandcamp.com")
            for album_stub in albums:
                link = album_stub.uri
                if link and link not in seen_links:
                    seen_links.add(link)
                    releases.append({
                        "title_full": f"{album_stub.work.title} by {artist_name}",
                        "artist": artist_name,
                        "album_title": album_stub.work.title,
                        "image": album_stub.image or "",
                        "description": f"New release from {artist_name}.",
                        "tags": [subdomain],
                        "link": link,
                        "genre": "artist/label",
                        "release_date": None,  # заполним позже
                    })
            if releases:
                break
        except Exception as e:
            print(f"   ⚠️ {url}: {e}")

    # Если artist_albums не сработал, пробуем через search_tag по артисту
    if not releases:
        try:
            for release in BandCamp.search_albums(f"artist:{subdomain}"):
                link = release.uri
                if link and link not in seen_links:
                    seen_links.add(link)
                    credits = release.work.credits[0].entity.name if release.work.credits else artist_name
                    releases.append({
                        "title_full": f"{release.work.title} by {credits}",
                        "artist": credits,
                        "album_title": release.work.title,
                        "image": release.image or "",
                        "description": f"New release from {artist_name}.",
                        "tags": [subdomain],
                        "link": link,
                        "genre": "artist/label",
                        "release_date": None,
                    })
        except Exception as e:
            print(f"   ⚠️ search_albums: {e}")

    print(f"   Найдено релизов: {len(releases)}")
    return releases


def fetch_releases_from_genre(genre):
    """Собирает релизы по жанровому тегу через py_bandcamp.search_tag."""
    genre_clean = genre.strip().lower().replace(" ", "-")
    if not genre_clean:
        return []

    print(f" 🏷️ [ЖАНР]: {genre_clean}...")
    releases = []
    seen_links = set()

    try:
        for release in BandCamp.search_tag(genre_clean, albums=True, tracks=False, max_pages=2):
            link = release.uri
            if not link or link in seen_links:
                continue
            seen_links.add(link)

            artist = release.work.credits[0].entity.name if release.work.credits else "Various Artists"
            releases.append({
                "title_full": f"{release.work.title} by {artist}",
                "artist": artist,
                "album_title": release.work.title,
                "image": release.image or "",
                "description": f"New release in #{genre_clean}.",
                "tags": [genre_clean],
                "link": link,
                "genre": genre_clean,
                "release_date": None,
            })
    except Exception as e:
        print(f"   ⚠️ search_tag: {e}")

    print(f"   Найдено релизов: {len(releases)}")
    return releases


def enrich_release_date(release):
    """Дозапрашивает полную информацию о релизе (включая дату) через album_to_release."""
    link = release.get("link")
    if not link:
        return release

    try:
        detailed = BandCamp.album_to_release(link, include_tracklist=False)

        # Дата
        if detailed.release_date:
            try:
                release["release_date"] = datetime.strptime(detailed.release_date, "%Y-%m-%d").date()
            except ValueError:
                release["release_date"] = None

        # Обновляем метаданные, если они были пустыми
        if not release.get("image") and detailed.image:
            release["image"] = detailed.image
        if detailed.work.title:
            release["album_title"] = detailed.work.title
            credits = detailed.work.credits[0].entity.name if detailed.work.credits else release["artist"]
            release["artist"] = credits
            release["title_full"] = f"{release['album_title']} by {credits}"

        # Теги из content_genres, если есть
        if detailed.work.content_genres:
            extra_tags = [g for g in detailed.work.content_genres if isinstance(g, str)]
            for t in extra_tags:
                if t not in release["tags"]:
                    release["tags"].append(t)

    except Exception as e:
        print(f"      ⚠️ album_to_release: {e}")

    return release


def is_release_date_valid(release_date):
    if release_date is None:
        print("   ⚠️ Дата релиза не найдена, пропуск.")
        return False

    today = datetime.now(timezone.utc).date()
    days_diff = (today - release_date).days

    if days_diff < 0:
        is_ok = ALLOW_UPCOMING
        print(f"   📅 Дата релиза: {release_date} ({'Анонс / Предзаказ' if is_ok else 'Пропущен (предзаказ)'})")
        return is_ok
    is_ok = days_diff <= MAX_DAYS_AGO
    print(f"   📅 Дата релиза: {release_date} ({f'{days_diff} дн. назад' if is_ok else f'Устарел ({days_diff} дн. назад)'})")
    return is_ok


# ==============================================================================
# TELEGRAM
# ==============================================================================
def send_to_telegram(release):
    import html as html_mod
    import requests

    if not TELEGRAM_BOT_TOKEN:
        print(" Ошибка: TELEGRAM_BOT_TOKEN не задан")
        return False

    title = html_mod.escape(release["title_full"])
    desc = html_mod.escape(release["description"])

    tags_list = [f"#{t.lower().replace('-', '_').replace(' ', '_')}" for t in release["tags"]]
    main_hashtag = f"#{release['genre'].lower().replace('-', '_').replace(' ', '_')}"
    if main_hashtag not in tags_list and release['genre'] != "artist/label":
        tags_list.insert(0, main_hashtag)
    tags_str = " ".join(tags_list)

    rd = release.get("release_date")
    date_line = f"📅 Дата релиза: <b>{rd.strftime('%d %b %Y')}</b>\n\n" if rd else ""

    caption = (
        f"🌌 <b>{title}</b>\n\n"
        f"{date_line}"
        f"📝 <i>{desc}</i>\n\n"
        f"🏷️ {tags_str}\n\n"
        f"🔗 <a href=\"{release['link']}\">Слушать / Купить на Bandcamp</a>"
    )

    if release.get("image"):
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "photo": release["image"],
                   "caption": caption, "parse_mode": "HTML"}
    else:
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": caption,
                   "parse_mode": "HTML", "disable_web_page_preview": False}

    try:
        return requests.post(api_url, data=payload, timeout=20).ok
    except Exception as e:
        print(f"❌ Telegram: {e}")
        return False


# ==============================================================================
# MAIN
# ==============================================================================
def main():
    if not TELEGRAM_BOT_TOKEN:
        print("Ошибка: Не задан TELEGRAM_BOT_TOKEN")
        return

    print("✅ py_bandcamp запущен с транспортом curl_cffi")

    init_csv_file()
    posted = load_posted()

    releases = []
    seen_links = set()

    # Сбор релизов
    for target in TARGET_ARTISTS_AND_LABELS:
        if target.strip():
            for r in fetch_releases_from_artist(target):
                if r["link"] not in seen_links:
                    seen_links.add(r["link"])
                    releases.append(r)

    for genre in GENRES:
        if genre.strip():
            for r in fetch_releases_from_genre(genre):
                if r["link"] not in seen_links:
                    seen_links.add(r["link"])
                    releases.append(r)

    print(f"\n🔎 Всего найдено релизов: {len(releases)}")

    new_releases = [r for r in releases if r["link"] not in posted]
    print(f"✨ Новых (не публиковались): {len(new_releases)}")

    if not new_releases:
        print("🏁 Нет новых релизов для обработки.")
        return

    # Обогащение датами
    print(f"\n📅 Получаем даты релизов для {len(new_releases)} релизов...")
    for i, release in enumerate(new_releases, 1):
        print(f"   [{i}/{len(new_releases)}] {release['title_full']}...")
        enrich_release_date(release)
        if release["release_date"]:
            print(f"      ✅ {release['release_date']}")
        else:
            print(f"      ❌ Дата не найдена")
        time.sleep(random.uniform(*DELAY_BETWEEN_RELEASES))

    # Сортировка по дате (свежие сверху)
    new_releases.sort(
        key=lambda x: x["release_date"] if x["release_date"] is not None else date.min,
        reverse=True
    )

    print("\n📊 Итоговый порядок:")
    for r in new_releases:
        d = r['release_date'].strftime('%Y-%m-%d') if r['release_date'] else 'Дата неизвестна'
        print(f"   • {d} — {r['title_full']}")

    # Публикация
    new_posts = 0
    for release in new_releases:
        if new_posts >= MAX_POSTS_PER_RUN:
            print(f"\n🛑 Лимит {MAX_POSTS_PER_RUN} постов за запуск.")
            break

        print(f"\n🔍 Оценка: {release['title_full']}")
        if not is_release_date_valid(release.get("release_date")):
            continue

        print(f"🚀 Публикация: {release['title_full']}")
        if send_to_telegram(release):
            posted.add(release["link"])
            save_to_csv(release)
            new_posts += 1
            time.sleep(3)
        else:
            print(f"❌ Не удалось отправить: {release['link']}")

    save_posted(posted)
    print(f"\n🏁 Завершено. Опубликовано: {new_posts}")


if __name__ == "__main__":
    main()
