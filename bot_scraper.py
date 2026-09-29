import os
import json
import time
import csv
import random
import html as html_mod
import re
from datetime import datetime, timezone, date
from urllib.parse import urljoin

# Транспорт для py_bandcamp (обход Fastly/Cloudflare)
os.environ["PYBANDCAMP_TRANSPORT"] = "curl_cffi"

import curl_cffi.requests as ccr

try:
    from py_bandcamp import BandCamp
    PYBANDCAMP_AVAILABLE = True
except ImportError:
    BandCamp = None
    PYBANDCAMP_AVAILABLE = False

# ==============================================================================
# НАСТРОЙКИ
# ==============================================================================
GENRES = [
    "ambient"
]

TARGET_ARTISTS_AND_LABELS = [
    # "https://sessionvictim.bandcamp.com"
]

MAX_DAYS_AGO = 2
ALLOW_UPCOMING = True

MAX_POSTS_PER_RUN = 5
POSTED_FILE = "posted_releases.json"
CSV_FILE = "releases_data.csv"

DELAY_BETWEEN_RELEASES = (1.0, 2.0)
DELAY_BETWEEN_PAGES = (2.0, 3.5)
MAX_PAGES_PER_ARTIST = 30
MAX_GENRE_PAGES = 3           # сколько страниц search_tag листать
MAX_GENRE_RELEASES = 60       # максимум кандидатов по жанру

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@bc_ambient")

DEBUG = True

# Заголовки-заглушки Cloudflare, которые нельзя использовать как название релиза
CF_TITLE_MARKERS = (
    "client challenge",
    "just a moment",
    "attention required",
    "access denied",
    "checking your browser",
    "please wait",
    "ddos protection",
)

# ==============================================================================
# HTTP-ЗАГОЛОВКИ
# ==============================================================================
def _headers_html():
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://bandcamp.com/",
    }


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


def _is_cf_title(t):
    if not t:
        return True
    low = t.lower().strip()
    return any(m in low for m in CF_TITLE_MARKERS)


def _strip_tags(text):
    if not text:
        return ""
    text = re.sub(r'<script[^>]*>.*?</script>', ' ', text, flags=re.I | re.S)
    text = re.sub(r'<style[^>]*>.*?</style>', ' ', text, flags=re.I | re.S)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html_mod.unescape(text)
    return re.sub(r'\s+', ' ', text).strip()


def _try_parse_date_string(d_str):
    if not d_str:
        return None
    s = str(d_str).strip()
    if not s:
        return None

    s = re.sub(r'[T\s]\d{1,2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+\-]\d{2}:?\d{2})?.*$', '', s).strip()
    s = s.rstrip('Z').strip().rstrip(',').strip()

    for fmt in ("%d %b %Y", "%d %B %Y", "%B %d, %Y", "%b %d, %Y",
                "%B %d %Y", "%b %d %Y", "%Y-%m-%d", "%Y/%m/%d",
                "%d.%m.%Y", "%d-%m-%Y", "%Y.%m.%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass

    for pat, fmts in (
        (r'(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})', ("%d %b %Y", "%d %B %Y")),
        (r'([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})', ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y")),
        (r'(\d{4}-\d{2}-\d{2})', ("%Y-%m-%d",)),
        (r'(\d{4}/\d{2}/\d{2})', ("%Y/%m/%d",)),
    ):
        m = re.search(pat, str(d_str))
        if m:
            for fmt in fmts:
                try:
                    return datetime.strptime(m.group(1), fmt).date()
                except ValueError:
                    pass
    return None


def _find_date_in_dict(d, keys=("release_date", "album_release_date",
                                "original_release_date", "publish_date",
                                "datePublished", "releaseDate")):
    if isinstance(d, dict):
        for k, v in d.items():
            if k in keys and v:
                parsed = _try_parse_date_string(str(v))
                if parsed:
                    return parsed
        for v in d.values():
            found = _find_date_in_dict(v, keys)
            if found:
                return found
    elif isinstance(d, list):
        for item in d:
            found = _find_date_in_dict(item, keys)
            if found:
                return found
    return None


def parse_date_from_html(html_text):
    if not html_text:
        return None

    m = re.search(r'data-tralbum=["\']([^"\']+)["\']', html_text)
    if m:
        try:
            tr = json.loads(html_mod.unescape(m.group(1)))
            parsed = _find_date_in_dict(tr)
            if parsed:
                return parsed
        except Exception:
            pass

    for ld_raw in re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html_text, re.I | re.S
    ):
        try:
            ld_data = json.loads(ld_raw.strip())
            items = ld_data if isinstance(ld_data, list) else [ld_data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                for key in ("datePublished", "releaseDate"):
                    if item.get(key):
                        parsed = _try_parse_date_string(str(item[key]))
                        if parsed:
                            return parsed
                graph = item.get("@graph")
                if isinstance(graph, list):
                    for g in graph:
                        if isinstance(g, dict):
                            for key in ("datePublished", "releaseDate"):
                                if g.get(key):
                                    parsed = _try_parse_date_string(str(g[key]))
                                    if parsed:
                                        return parsed
        except Exception:
            pass

    for dt in re.findall(r'<time[^>]*datetime=["\']([^"\']+)["\']', html_text, re.I):
        parsed = _try_parse_date_string(dt)
        if parsed:
            return parsed

    m = re.search(
        r'<meta\s+name=["\']bc-page-properties["\']\s+content=["\']([^"\']+)["\']',
        html_text, re.I
    )
    if m:
        try:
            pj = json.loads(html_mod.unescape(m.group(1)))
            parsed = _find_date_in_dict(pj)
            if parsed:
                return parsed
        except Exception:
            pass

    m = re.search(
        r'class=["\'][^"\']*tralbum-credits[^"\']*["\'][^>]*>(.*?)</div>',
        html_text, re.I | re.S
    )
    if m:
        bt = _strip_tags(m.group(1))
        dm = re.search(
            r'released?\s+(?:on\s+)?([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})',
            bt, re.I
        )
        if dm:
            parsed = _try_parse_date_string(dm.group(1))
            if parsed:
                return parsed

    for meta_re in (
        r'<meta\s+property=["\']og:description["\']\s+content=["\']([^"\']+)["\']',
        r'<meta\s+name=["\']description["\']\s+content=["\']([^"\']+)["\']',
    ):
        m = re.search(meta_re, html_text, re.I)
        if m:
            desc = html_mod.unescape(m.group(1))
            dm = re.search(
                r'released?\s+(?:on\s+)?([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})',
                desc, re.I
            )
            if dm:
                parsed = _try_parse_date_string(dm.group(1))
                if parsed:
                    return parsed

    text_only = _strip_tags(html_text)
    for pat in (
        r'released?\s+(?:on\s+)?([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})',
        r'released?\s+(?:on\s+)?(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})',
    ):
        m = re.search(pat, text_only, re.I)
        if m:
            parsed = _try_parse_date_string(m.group(1))
            if parsed:
                return parsed

    for pat in (
        r'itemprop=["\']datePublished["\'][^>]*content=["\']([^"\']+)["\']',
        r'content=["\']([^"\']+)["\'][^>]*itemprop=["\']datePublished["\']',
    ):
        m = re.search(pat, html_text, re.I)
        if m:
            parsed = _try_parse_date_string(m.group(1))
            if parsed:
                return parsed

    for raw in re.findall(
        r'(?:album_release_date|release_date|current_release_date|original_release_date)'
        r'"?\s*[:=]\s*"([^"]+)"',
        html_text, re.I
    ):
        parsed = _try_parse_date_string(raw)
        if parsed:
            return parsed

    return None


def _extract_og(html_text, prop):
    m = re.search(
        rf'<meta\s+property=["\']og:{prop}["\']\s+content=["\']([^"\']*)["\']',
        html_text, re.I
    )
    if m:
        return html_mod.unescape(m.group(1)).strip()
    return ""


def _title_from_tralbum(html_text):
    m = re.search(r'data-tralbum=["\']([^"\']+)["\']', html_text)
    if not m:
        return ""
    try:
        tr = json.loads(html_mod.unescape(m.group(1)))
    except Exception:
        return ""

    current = tr.get("current") or {}
    for key in ("title", "album_title"):
        if isinstance(current, dict) and current.get(key):
            return str(current[key]).strip()
    for key in ("album_title", "title"):
        if tr.get(key):
            return str(tr[key]).strip()
    return ""


def _title_from_url(url):
    try:
        slug = url.rstrip("/").split("/")[-1].split("?")[0]
        return slug.replace("-", " ").replace("_", " ").title()
    except Exception:
        return ""


def _extract_title(html_text, url=""):
    title = _title_from_tralbum(html_text)
    if title and not _is_cf_title(title):
        return title

    og_title = _extract_og(html_text, "title")
    if og_title and not _is_cf_title(og_title):
        return og_title.split("|")[0].strip()

    m = re.search(r'<title>([^<]+)</title>', html_text, re.I)
    if m:
        t = html_mod.unescape(m.group(1)).split("|")[0].strip()
        if t and not _is_cf_title(t):
            return t

    return _title_from_url(url)


def _extract_artist_from_page(html_text):
    a = _extract_og(html_text, "site_name")
    if a and not _is_cf_title(a):
        return a.split("|")[0].strip()
    return ""


# ==============================================================================
# ПАРСИНГ АРТИСТОВ / ЛЕЙБЛОВ
# ==============================================================================
def _collect_release_paths(subdomain, session):
    base = f"https://{subdomain}.bandcamp.com"
    all_paths = set()
    artist_name = subdomain

    for page in range(1, MAX_PAGES_PER_ARTIST + 1):
        url = f"{base}/music" if page == 1 else f"{base}/music?page={page}"
        print(f"   📄 Страница {page}: {url}")

        try:
            r = session.get(url, timeout=30)
        except Exception as e:
            print(f"      ⚠️ {e}")
            break

        if r.status_code != 200:
            print(f"      ⚠️ HTTP {r.status_code}")
            break

        html_text = r.text

        if page == 1:
            artist_name = _extract_artist_from_page(html_text) or subdomain
            if not artist_name or artist_name == subdomain:
                cm = re.search(r'data-client-items="([^"]+)"', html_text)
                if cm:
                    try:
                        items = json.loads(html_mod.unescape(cm.group(1)))
                        if items:
                            band = items[0].get("band_name") or items[0].get("band")
                            if band:
                                artist_name = band
                    except Exception:
                        pass
            print(f"   Артист: {artist_name}")

        new_paths = set()
        for m in re.finditer(
            r'(?:href="|"page_url"\s*:\s*"|"title_link"\s*:\s*")(/(?:album|track)/[a-zA-Z0-9_\-]+)',
            html_text
        ):
            path = m.group(1).split("?")[0]
            if path not in all_paths:
                new_paths.add(path)
                all_paths.add(path)

        print(f"      Новых путей: {len(new_paths)} (всего {len(all_paths)})")

        if not new_paths:
            print(f"      🏁 Новых путей нет, завершаем пагинацию.")
            break

        time.sleep(random.uniform(*DELAY_BETWEEN_PAGES))

    return all_paths, artist_name


def _try_pybandcamp_fallback(url):
    if not PYBANDCAMP_AVAILABLE:
        return None
    try:
        release = BandCamp.album_to_release(url, include_tracklist=False)
        raw = getattr(release, "release_date", None)
        if raw:
            return _try_parse_date_string(str(raw))
    except Exception:
        pass
    return None


def fetch_releases_from_artist(target):
    subdomain = extract_subdomain(target)
    if not subdomain:
        return []

    base = f"https://{subdomain}.bandcamp.com"
    print(f" 👤 [АРТИСТ/ЛЕЙБЛ]: {subdomain} ({base})...")

    releases = []

    with ccr.Session(impersonate="chrome120") as session:
        print(f"\n   ── Этап 1: сбор путей с /music ──")
        paths, artist_name = _collect_release_paths(subdomain, session)
        print(f"   Всего путей: {len(paths)}")

        if not paths:
            print("   ⚠️ Не удалось собрать ни одного релиза.")
            return []

        print(f"\n   ── Этап 2: парсинг страниц релизов ──")
        for i, path in enumerate(sorted(paths), 1):
            url = f"{base}{path}"
            print(f"\n   [{i}/{len(paths)}] {url}")

            try:
                r = session.get(url, timeout=30)
            except Exception as e:
                print(f"      ⚠️ {e}")
                continue

            if r.status_code != 200:
                print(f"      ⚠️ HTTP {r.status_code}")
                continue

            html_text = r.text
            release_date = parse_date_from_html(html_text)

            if not release_date:
                release_date = _try_pybandcamp_fallback(url)

            title = _extract_title(html_text, url=url)
            image = _extract_og(html_text, "image")
            page_artist = _extract_artist_from_page(html_text)
            credits_artist = page_artist or artist_name

            if DEBUG:
                print(f"      🔍 release_date = {release_date!r}, title = {title!r}")

            releases.append({
                "title_full": f"{title} by {credits_artist}",
                "artist": credits_artist,
                "album_title": title,
                "image": image,
                "description": f"New release from {credits_artist}.",
                "tags": [subdomain],
                "link": url,
                "genre": "artist/label",
                "release_date": release_date,
            })

            print(f"      ✅ {title} — {release_date or 'дата не найдена'}")

            time.sleep(random.uniform(*DELAY_BETWEEN_RELEASES))

    print(f"\n   ИТОГО релизов у {artist_name}: {len(releases)}")
    return releases


# ==============================================================================
# ПАРСИНГ ЖАНРОВ / ТЕГОВ — через py_bandcamp.search_tag
# ==============================================================================
def _release_date_from_pybandcamp(release):
    """Достаёт дату из объекта Release py_bandcamp.

    В зависимости от версии release_date может быть:
      - строкой ISO 'YYYY-MM-DD'
      - pydantic-объектом IsoDate с year/month/day
      - None
    """
    raw = getattr(release, "release_date", None)
    if not raw:
        return None
    if hasattr(raw, "year") and hasattr(raw, "month") and hasattr(raw, "day"):
        try:
            return date(int(raw.year), int(raw.month), int(raw.day))
        except Exception:
            pass
    return _try_parse_date_string(str(raw))


def fetch_releases_from_genre(genre):
    genre_clean = genre.strip().lower().replace(" ", "-")
    if not genre_clean:
        return []

    print(f" 🏷️ [ЖАНР]: {genre_clean}...")
    releases = []
    seen_links = set()

    if not PYBANDCAMP_AVAILABLE:
        print("   ⚠️ py_bandcamp недоступен. Установите: pip install py_bandcamp")
        return []

    print(f"\n   ── Этап 1: поиск через py_bandcamp.search_tag ──")

    # Пробуем разные варианты вызова: с max_pages и без (для разных версий API)
    items = []
    try:
        print(f"   🔎 search_tag({genre_clean!r}, albums=True, tracks=False, max_pages={MAX_GENRE_PAGES})")
        items = list(BandCamp.search_tag(
            genre_clean,
            albums=True,
            tracks=False,
            max_pages=MAX_GENRE_PAGES,
        ))
    except TypeError as e:
        # В некоторых версиях max_pages не поддерживается
        print(f"      ⚠️ {e} — пробуем без max_pages")
        try:
            items = list(BandCamp.search_tag(genre_clean, albums=True, tracks=False))
        except Exception as e2:
            print(f"      ⚠️ search_tag: {e2}")
            items = []
    except Exception as e:
        print(f"      ⚠️ search_tag: {e}")
        items = []

    print(f"   Получено объектов Release: {len(items)}")

    if not items:
        print("   ⚠️ search_tag не вернул результатов.")
        return []

    print(f"\n   ── Этап 2: обработка релизов ──")

    for i, release in enumerate(items, 1):
        if len(releases) >= MAX_GENRE_RELEASES:
            print(f"   🛑 Достигнут лимит {MAX_GENRE_RELEASES} релизов, останавливаемся.")
            break

        try:
            link = release.uri
        except Exception:
            link = None

        if not link or link in seen_links:
            continue
        seen_links.add(link)

        title = "Без названия"
        try:
            if release.work and release.work.title:
                title = release.work.title
        except Exception:
            pass

        artist = "Various Artists"
        try:
            if release.work and release.work.credits:
                artist = release.work.credits[0].entity.name or "Various Artists"
        except Exception:
            pass

        image = getattr(release, "image", "") or ""
        release_date = _release_date_from_pybandcamp(release)

        if DEBUG:
            print(f"   [{i}/{len(items)}] {title} — release_date={release_date!r}")

        releases.append({
            "title_full": f"{title} by {artist}",
            "artist": artist,
            "album_title": title,
            "image": image,
            "description": f"New release in #{genre_clean}.",
            "tags": [genre_clean],
            "link": link,
            "genre": genre_clean,
            "release_date": release_date,
        })

        print(f"      ✅ {title} — {release_date or 'дата не найдена'}")

        time.sleep(random.uniform(*DELAY_BETWEEN_RELEASES))

    print(f"\n   ИТОГО релизов в жанре '{genre_clean}': {len(releases)}")
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
    import requests as req

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
        return req.post(api_url, data=payload, timeout=20).ok
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

    print("✅ Скрипт запущен (curl_cffi + HTML-парсинг дат)")

    if not PYBANDCAMP_AVAILABLE:
        print("⚠️ py_bandcamp не установлен. Установите: pip install py_bandcamp")
        print("   Поиск по артистам продолжит работать, по жанрам — нет.")

    init_csv_file()
    posted = load_posted()

    releases = []
    seen_links = set()

    # Артисты/лейблы
    for target in TARGET_ARTISTS_AND_LABELS:
        if target.strip():
            for r in fetch_releases_from_artist(target):
                if r["link"] not in seen_links:
                    seen_links.add(r["link"])
                    releases.append(r)

    # Жанры
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
