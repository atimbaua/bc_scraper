# Bandcamp Telegram Scraper Bot

Бот для автоматического поиска новых релизов на Bandcamp по жанрам и заданным артистам/лейблам, с публикацией в Telegram-канал.

---

## Основные возможности

* **Поиск по жанрам** — через внутренний Discover API Bandcamp (`/api/discover/3/get_web`), до 48 релизов за один запрос.
* **Поиск по артистам и лейблам** — обход страницы `/music` с пагинацией, парсинг каждой страницы релиза.
* **Точные даты релизов** — многоуровневый парсер: `data-tralbum` → JSON-LD → `<time>` → `bc-page-properties` → `tralbum-credits` → мета-теги.
* **Обход Cloudflare/Fastly** — через `curl_cffi` с имперсонацией Chrome. Одна долгоживущая сессия для всех запросов.
* **Фильтрация по дате** — публикуются только релизы не старше `MAX_DAYS_AGO` дней (опционально включаются предзаказы).
* **Защита от повторных публикаций** — состояние хранится в `posted_releases.json`.
* **Экспорт в CSV** — все опубликованные релизы логируются в `releases_data.csv`.
* **Fallback через** `py_bandcamp` — если HTML-парсинг не дал дату, используется библиотека.

---

## Структура проекта

```text
.
├── .github/
│   └── workflows/
│       └── scraper.yml       # Настройка расписания запускa GitHub Actions
├── bot_scraper.py            # Основной код парсера и Telegram-бота
├── posted_releases.json      # Список уже опубликованных ссылок (JSON)
├── releases_data.csv         # Накопленная база данных релизов (CSV)
└── README.md                 # Документация проекта
```

---

## Настройка бота (`bot_scraper.py`)

В начале файла `bot_scraper.py` вы можете настроить критерии поиска:

```python
# 1. Жанры для поиска (можно указать один или несколько)
GENRES = ["ambient", "downtempo"]

# 2. Артисты и лейблы для отслеживания (указывайте поддомен или полную ссылку)
TARGET_ARTISTS_AND_LABELS = [
    "carbonbasedlifeforms",
    "https://ultimae.bandcamp.com",
    "solarfields"
]

MAX_DAYS_AGO = 2          # публиковать не старше N дней
ALLOW_UPCOMING = True     # включать предзаказы

MAX_POSTS_PER_RUN = 5     # лимит публикаций за один запуск

DELAY_BETWEEN_RELEASES = (1.0, 2.0)   # пауза между релизами (сек)
DELAY_BETWEEN_PAGES = (2.0, 3.5)      # пауза между страницами артиста

MAX_GENRE_RELEASES = 60   # максимум кандидатов по жанру
MAX_PAGES_PER_ARTIST = 30 # максимум страниц /music

# True  — даты берутся из Discover API (быстро, один запрос)
# False — даты парсятся со страниц релизов (медленно, точные релизные даты)
GENRE_USE_API_DATE = True
```

---

## Переменные окружения (Secrets)

Для работы бота в GitHub Actions или локально требуются следующие переменные окружения:

| Переменная | Описание | Обязательна? |
| :--- | :--- | :---: |
| `TELEGRAM_BOT_TOKEN` | Токен Telegram-бота, полученный у [@BotFather](https://t.me/BotFather) | **Да** |
| `TELEGRAM_CHAT_ID` | Имя канала или его ID | **Да** |
| `SCRAPERAPI_KEY` | API-ключ сервиса [ScraperAPI](https://www.scraperapi.com/) (резервный канал) | Нет |

---

## Быстрый старт и развертывание

1. **Склонируйте репозиторий:**
   ```bash
   git clone https://github.com/<your-username>/bc_scraper.git
   cd bc_scraper
   ```

2. **Установите зависимости:**
   ```bash
   pip install requests beautifulsoup4 curl_cffi
   pip install py_bandcamp
   ```

3. **Настройте Secrets в GitHub:**
   * Перейдите в **Settings** ➔ **Secrets and variables** ➔ **Actions**.
   * Нажмите **New repository secret**.
   * Добавьте `TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHAT_ID`.

4. **Запустите Workflow:**
   * Перейдите во вкладку **Actions**.
   * Бот будет запускаться по указанному расписанию. Вы также можете запустить его вручную кнопкой **Run workflow**.

---

## Структура CSV-файла (`releases_data.csv`)

Все опубликованные релизы сохраняются в файле `releases_data.csv` со следующей структурой полей:

| Поле | Описание | Пример |
| :--- | :--- | :--- |
| `published_at_utc` | Дата и время публикации (UTC) | `2026-09-28 08:30:00` |
| `genre` | Категория / Жанр релиза | `ambient` (или `artist/label`) |
| `artist` | Исполнитель / Лейбл | `Carbon Based Lifeforms` |
| `album_title` | Название альбома или трека | `World of Sleepers` |
| `url` | Нормализованная ссылка | `https://carbonbasedlifeforms.bandcamp.com/album/world-of-sleepers` |
| `tags` | Теги релиза | `ambient, downtempo` |
| `image_url` | Прямая ссылка на обложку | `https://f4.bcbits.com/img/a1234567890_10.jpg` |

---

## Лицензия

Проект распространяется под лицензией **MIT**.
Используйте на здоровье, но уважайте Bandcamp: не отправляйте слишком частые запросы и оставляйте задержки между ними.
