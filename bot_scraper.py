import os
import json
import time
import csv
import random
import html as html_mod
from datetime import datetime, timezone, date
from urllib.parse import urljoin

# ==============================================================================
# НАСТРОЙКА ТРАНСПОРТА (обход Cloudflare/Fastly)
# ==============================================================================
# ВАЖНО: переменная должна быть установлена ДО импорта py_bandcamp
os.environ["PYBANDCAMP_TRANSPORT"] = "curl_cffi"

from py_bandcamp import (
    BandCamp,
    BandcampArtist,
    BandcampAlbum,
)

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
DELAY_BETWEEN_PAGES = (3.0, 5.0)
MAX_PAGES_PER_ARTIST = 30          # защита от бесконечного цикла при пагинации
SEARCH_MAX_RESULTS = 100           # ограничение на fallback-поиск

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@bc_ambient")

DEBUG = True                       # печатать raw release_date для каждого релиза

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
# ХЕЛПЕРЫ
# ==============================================================================
def extract_subdomain(target):
    target = target.strip().lower()
    target = target.replace("https://", "").replace("http://", "")
    return target.split('.')[0].split('/')[0]


def _release_date_to_date(release_date_value):
    """Конвертирует дату из py_bandcamp в datetime.date."""
    if not release_date_value:
        return None

    if isinstance(release_date_value, datetime):
        return release_date_value.date()
    if isinstance(release_date_value, date):
        return release_date_value

    if (hasattr(release_date_value, "year")
            and hasattr(release_date_value, "month")
            and hasattr(release_date_value, "day")):
        try:
            return date(
                int(release_date_value.year),
                int(release_date_value.month),
                int(release_date_value.day),
            )
        except Exception:
            pass

    s = str(release_date_value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d %b %Y", "%d %B %Y",
                "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def _safe_get(obj, attr, default=None):
    try:
        return getattr(obj, attr, default)
    except Exception:
        return default


def _absolute_album_url(album, base_url):
    """Возвращает абсолютный URL страницы релиза.

    album.url может быть относительным (/album/xxx) или абсолютным.
    Соединяем с базовым доменом артиста (без ?page=N), чтобы не получить
    мусор вида .../music?page=1/album/xxx.
    """
    raw = getattr(album, "url", None)
    if not raw:
        raw = str(album)
    if not raw:
        return None
    if raw.startswith("http"):
        return raw
    # urljoin с базовым URL без завершающего слэша даст корректный абсолютный путь
    return urljoin(base_url.rstrip("/") + "/", raw.lstrip("/"))


def _release_from_album_obj(album, artist_name, subdomain, base_url):
    """Собирает словарь релиза из BandcampAlbum.

    Передаём АБСОЛЮТНЫЙ URL строкой в album_to_release, чтобы py_bandcamp
    загрузил страницу релиза и извлёк release_date.
    """
    album_url = _absolute_album_url(album, base_url)
    if not album_url or not album_url.startswith("http"):
        print(f"      ⚠️ Не удалось получить URL релиза: {album!r}")
        return None

    try:
        release = BandCamp.album_to_release(album_url, include_tracklist=False)
    except Exception as e:
        print(f"      ⚠️ album_to_release({album_url}): {e}")
        return None

    link = release.uri
    if not link:
        return None

    title = release.work.title or "Без названия"
    credits_artist = artist_name
    if release.work.credits:
        try:
            credits_artist = release.work.credits[0].entity.name or artist_name
        except Exception:
            pass

    raw_date = _safe_get(release, "release_date")
    if DEBUG:
        print(f"      🔍 raw release_date = {raw_date!r} ({type(raw_date).__name__})")

    release_date = _release_date_to_date(raw_date)

    return {
        "title_full": f"{title} by {credits_artist}",
        "artist": credits_artist,
        "album_title": title,
        "image": _safe_get(release, "image") or "",
        "description": f"New release from {credits_artist}.",
        "tags": [subdomain],
        "link": link,
        "genre": "artist/label",
        "release_date": release_date,
    }


def _release_from_release_obj(release, subdomain, default_artist="Various Artists"):
    """Собирает словарь релиза из объекта Release (возвращается search_albums)."""
    link = _safe_get(release, "uri")
    if not link:
        return None

    title = "Без названия"
    if release.work and release.work.title:
        title = release.work.title

    artist = default_artist
    if release.work and release.work.credits:
        try:
            artist = release.work.credits[0].entity.name or default_artist
        except Exception:
            pass

    release_date = _release_date_to_date(_safe_get(release, "release_date"))

    return {
        "title_full": f"{title} by {artist}",
        "artist": artist,
        "album_title": title,
        "image": _safe_get(release, "image") or "",
        "description": f"New release from {artist}.",
        "tags": [subdomain],
        "link": link,
        "genre": "artist/label",
        "release_date": release_date,
    }


# ==============================================================================
# СБОР РЕЛИЗОВ — ЧАСТЬ 1: ПАГИНАЦИЯ /music?page=N
# ==============================================================================
def _crawl_music_pages(artist_base_url, subdomain):
    """Обходит /music?page=N и возвращает список релизов.

    Возвращает (releases, artist_name, seen_links).
    """
    print(f"\n   ── Этап 1: обход /music?page=N ──")
    releases = []
    seen_links = set()
    artist_name = subdomain

    for page in range(1, MAX_PAGES_PER_ARTIST + 1):
        music_url = f"{artist_base_url}/music?page={page}"
        print(f"\n   📄 Страница {page}: {music_url}")

        try:
            artist = BandcampArtist.from_url(music_url)
        except Exception as e:
            print(f"      ⚠️ Не удалось получить страницу {page}: {e}")
            break

        if page == 1 and artist.name:
            artist_name = artist.name
            print(f"   Артист: {artist_name}")

        try:
            page_albums = artist.albums
        except Exception as e:
            print(f"      ⚠️ artist.albums на странице {page}: {e}")
            page_albums = []

        if not page_albums:
            print(f"      🏁 Страница {page} пуста, завершаем обход.")
            break

        print(f"      Найдено на странице: {len(page_albums)}")

        new_on_page = 0
        for album in page_albums:
            album_url = _absolute_album_url(album, artist_base_url)
            if not album_url or album_url in seen_links:
                continue
            seen_links.add(album_url)
            new_on_page += 1

            release = _release_from_album_obj(album, artist_name, subdomain, artist_base_url)
            if not release:
                continue

            releases.append(release)
            print(f"      ✅ {release['album_title']} — "
                  f"{release['release_date'] or 'дата не указана'}")

            time.sleep(random.uniform(*DELAY_BETWEEN_RELEASES))

        if new_on_page == 0:
            print(f"      🏁 На странице {page} нет новых релизов, завершаем пагинацию.")
            break

        time.sleep(random.uniform(*DELAY_BETWEEN_PAGES))

    print(f"\n   Итого после пагинации: {len(releases)}")
    return releases, artist_name, seen_links


# ==============================================================================
# СБОР РЕЛИЗОВ — ЧАСТЬ 2: FALLBACK ЧЕРЕЗ search_albums
# ==============================================================================
def _fallback_search_albums(artist_name, subdomain, seen_links):
    """Добирает релизы через BandCamp.search_albums.

    В текущей версии py_bandcamp search_albums не принимает max_pages,
    поэтому просто итерируемся по результатам с ограничением SEARCH_MAX_RESULTS.
    """
    print(f"\n   ── Этап 2: fallback через search_albums ──")
    releases = []
    query = f"artist:{artist_name}"
    print(f"   🔎 Поисковый запрос: {query!r}")

    try:
        results = list(BandCamp.search_albums(query))
    except Exception as e:
        print(f"      ⚠️ search_albums: {e}")
        return releases

    count_total = 0
    count_new = 0
    count_by_us = 0

    for release in results[:SEARCH_MAX_RESULTS]:
        count_total += 1
        link = _safe_get(release, "uri")
        if not link:
            continue

        # Отфильтровываем релизы других артистов: проверяем, что ссылка
        # ведёт на поддомен нашего артиста.
        if f"//{subdomain}.bandcamp.com/" not in link:
            continue
        count_by_us += 1

        if link in seen_links:
            continue
        seen_links.add(link)
        count_new += 1

        new_release = _release_from_release_obj(release, subdomain, default_artist=artist_name)
        if not new_release:
            continue

        releases.append(new_release)
        print(f"      ✅ {new_release['album_title']} — "
              f"{new_release['release_date'] or 'дата не указана'}")

        time.sleep(random.uniform(*DELAY_BETWEEN_RELEASES))

    print(f"   Всего из поиска: {count_total}, "
          f"от нашего артиста: {count_by_us}, новых: {count_new}")
    return releases


# ==============================================================================
# СБОР РЕЛИЗОВ С АРТИСТА/ЛЕЙБЛА (ОБА ЭТАПА)
# ==============================================================================
def fetch_releases_from_artist(target):
    """Собирает все релизы артиста: пагинация + fallback через search_albums."""
    subdomain = extract_subdomain(target)
    if not subdomain:
        return []

    artist_base_url = f"https://{subdomain}.bandcamp.com"
    print(f" 👤 [АРТИСТ/ЛЕЙБЛ]: {subdomain} ({artist_base_url})...")

    # Этап 1: пагинация
    releases, artist_name, seen_links = _crawl_music_pages(artist_base_url, subdomain)

    # Этап 2: fallback через search_albums
    fallback_releases = _fallback_search_albums(artist_name, subdomain, seen_links)
    releases.extend(fallback_releases)

    print(f"\n   ИТОГО релизов у {artist_name}: {len(releases)} "
          f"(пагинация: {len(releases) - len(fallback_releases)}, "
          f"поиск: {len(fallback_releases)})")
    return releases


def fetch_releases_from_genre(genre):
    """Собирает релизы по жанровому тегу."""
    genre_clean = genre.strip().lower().replace(" ", "-")
    if not genre_clean:
        return []

    print(f" 🏷️ [ЖАНР]: {genre_clean}...")
    releases = []
    seen_links = set()

    try:
        for album in BandCamp.search_tag(genre_clean, albums=True, tracks=False, max_pages=2):
            album_url = _absolute_album_url(album, "https://bandcamp.com")
            if not album_url or not album_url.startswith("http"):
                continue
            if album_url in seen_links:
                continue
            seen_links.add(album_url)

            try:
                release = BandCamp.album_to_release(album_url, include_tracklist=False)
            except Exception:
                continue

            link = release.uri
            if not link:
                continue

            artist = "Various Artists"
            if release.work.credits:
                try:
                    artist = release.work.credits[0].entity.name or "Various Artists"
                except Exception:
                    pass

            release_date = _release_date_to_date(_safe_get(release, "release_date"))

            releases.append({
                "title_full": f"{release.work.title} by {artist}",
                "artist": artist,
                "album_title": release.work.title,
                "image": _safe_get(release, "image") or "",
                "description": f"New release in #{genre_clean}.",
                "tags": [genre_clean],
                "link": link,
                "genre": genre_clean,
                "release_date": release_date,
            })
    except Exception as e:
        print(f"   ⚠️ search_tag: {e}")

    print(f"   Найдено релизов: {len(releases)}")
    return releases


def is_release_date_valid(release_date):
    if release_date is None:
        print("   ⚠️ Дата релиза не найдена, пропуск.")
        return False

    today = datetime.now(timezone.utc).date()
    days_diff = (today - release_date).days

    if days_diff < 0:
        is_ok = ALLOW_UPCOMING
        print(f"   📅 Дата релиза: {release_date} "
              f"({'Анонс / Предзаказ' if is_ok else 'Пропущен (предзаказ)'})")
        return is_ok

    is_ok = days_diff <= MAX_DAYS_AGO
    status = f"{days_diff} дн. назад" if is_ok else f"Устарел ({days_diff} дн. назад)"
    print(f"   📅 Дата релиза: {release_date} ({status})")
    return is_ok


# ==============================================================================
# TELEGRAM
# ==============================================================================
def send_to_telegram(release):
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

    new_releases.sort(
        key=lambda x: x["release_date"] if x["release_date"] is not None else date.min,
        reverse=True
    )

    print("\n📊 Итоговый порядок:")
    for r in new_releases:
        d = r['release_date'].strftime('%Y-%m-%d') if r['release_date'] else 'Дата неизвестна'
        print(f"   • {d} — {r['title_full']}")

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
