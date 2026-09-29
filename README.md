# Bandcamp Telegram Scraper Bot

Автоматический Python-бот для поиска и публикации свежих музыкальных релизов с **Bandcamp** в Telegram-канал. 

Бот умеет отслеживать релизы как по **категориям/жанрам**, так и по **конкретным артистам или лейблам**.

Проект использует внутренние API Bandcamp и прямой парсинг страниц, обходит защиту Cloudflare с помощью имитации TLS-отпечатка браузера (`curl_cffi`) и работает полностью бесплатно на базе **GitHub Actions**.

---

## Основные возможности

* **Гибкий поиск по жанрам и артистам:** Поддержка списка жанров (`GENRES`) и конкретных поддоменов артистов/лейблов (`TARGET_ARTISTS_AND_LABELS`).
* **Прямая работа с API и веб-страницами:** Получение данных через Discover API Bandcamp и прямая проверка страниц `/music` нужных артистов.
* **Обход защиты Cloudflare:** Использование библиотеки `curl_cffi` с подменой TLS-fingerprint (Chrome 120) и каскадными фоллбэками.
* **Автоматическая нормализация ссылок:** Преобразование алиасов Bandcamp (`/a/`, `/t/`) в канонические ссылки (`/album/`, `/track/`).
* **Красивое оформление постов:** Публикация обложки альбома в высоком качестве, названия, артиста, автоматических хэштегов и прямой ссылки.
* **Защита от дубликатов:** Сохранение истории отправленных релизов в `posted_releases.json`.
* **Экспорт данных в CSV:** Накопление базы релизов (`releases_data.csv`) из 7 полей для последующего анализа и визуализации.
* **Автоматизация:** Публикация порциями (по умолчанию 3 релиза за запуск) по расписанию через GitHub Actions.

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

### Вариант 1. Автоматический запуск через GitHub Actions (Рекомендуется)

1. **Склонируйте репозиторий:**
   ```bash
   git clone https://github.com/your-username/your-repo-name.git
   cd your-repo-name
   ```

2. **Настройте Secrets в GitHub:**
   * Перейдите в **Settings** ➔ **Secrets and variables** ➔ **Actions**.
   * Нажмите **New repository secret**.
   * Добавьте `TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHAT_ID`.

3. **Запустите Workflow:**
   * Перейдите во вкладку **Actions**.
   * Бот будет запускаться каждый час по расписанию. Вы также можете запустить его вручную кнопкой **Run workflow**.

---

### Вариант 2. Локальный запуск или VPS

1. **Установите зависимости:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Задайте переменные окружения:**
   * **Linux / macOS:**
     ```bash
     export TELEGRAM_BOT_TOKEN="your_token_here"
     export TELEGRAM_CHAT_ID="@your_channel"
     ```
   * **Windows (CMD / PowerShell):**
     ```cmd
     set TELEGRAM_BOT_TOKEN=your_token_here
     set TELEGRAM_CHAT_ID=@your_channel
     ```

3. **Запустите скрипт:**
   ```bash
   python bot_scraper.py
   ```

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
