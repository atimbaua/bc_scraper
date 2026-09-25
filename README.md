# Bandcamp Telegram Scraper Bot

Автоматический Python-бот для поиска свежих музыкальных релизов на Bandcamp и их публикации в Telegram-канал. Жанр релиза устанавливается в переменной `GENRE`.

Проект использует публичные и внутренние API Bandcamp, обходит защиту Cloudflare с помощью имитации TLS-отпечатка браузера (`curl_cffi`) и работает полностью бесплатно на базе **GitHub Actions**.

---

## Основные возможности

- **Прямая работа с API Bandcamp:** Получение чистых JSON-данных без необходимости тяжелого парсинга HTML и рендеринга JS.
- **Обход защиты Cloudflare:** Использование библиотеки `curl_cffi` с подменой TLS-fingerprint (Chrome 120) и каскадными фоллбэками (ScraperAPI, requests).
- **Красивое оформление постов:** Публикация обложки альбома в высоком качестве (1000x1000 px), названия, артиста, автоматических хэштегов и прямой ссылки.
- **Защита от повторных публикаций:** Сохранение истории отправленных релизов в `posted_releases.json`.
- **Экспорт данных в CSV:** Накопление базы релизов (`releases_data.csv`) для последующей аналитики и визуализации (например, в Yandex DataLens).
- **Лимитирование и автоматизация:** Публикация порциями (по умолчанию 5 релизов за запуск) каждый час по расписанию через GitHub Actions.

---

## Структура проекта

```text
.
├── .github/
│   └── workflows/
│       └── scraper.yml       # Настройка расписания и запуска GitHub Actions
├── bot_scraper.py            # Основной код парсера и Telegram-бота
├── posted_releases.json      # Список уже опубликованных ссылок (JSON)
├── releases_data.csv         # Накопленная база данных релизов (CSV)
├── requirements.txt          # Зависимости Python
└── README.md                 # Документация проекта
```

---

## Переменные окружения (Secrets)

Для работы бота требуется настроить следующие переменные окружения:

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
   - Перейдите в **Settings** ➔ **Secrets and variables** ➔ **Actions**.
   - Нажмите **New repository secret**.
   - Добавьте `TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHAT_ID`.

3. **Включите Workflow:**
   - Перейдите во вкладку **Actions**.
   - Разрешите запуск workflows.
   - Скрипт будет автоматически запускаться каждый час, а также вы можете запустить его вручную кнопкой **Run workflow**.

---

### Вариант 2. Локальный запуск или VPS

1. **Клонируйте репозиторий и установите зависимости:**
   ```bash
   git clone https://github.com/your-username/your-repo-name.git
   cd your-repo-name
   pip install -r requirements.txt
   ```

2. **Задайте переменные окружения:**
   - **Linux / macOS:**
     ```bash
     export TELEGRAM_BOT_TOKEN="your_token_here"
     export TELEGRAM_CHAT_ID="@your_channel"
     ```
   - **Windows (CMD / PowerShell):**
     ```cmd
     set TELEGRAM_BOT_TOKEN=your_token_here
     set TELEGRAM_CHAT_ID=@your_channel
     ```

3. **Запустите скрипт:**
   ```bash
   python bot_scraper.py
   ```

---

## Зависимости (`requirements.txt`)

Для работы парсера используются следующие пакеты:

```text
requests>=2.31.0
beautifulsoup4>=4.12.0
curl_cffi>=0.6.0
```

---

## Структура сохраняемого CSV-файла

Скрипт ведет постоянный лог опубликованных альбомов в формате `releases_data.csv`:

| Поле | Описание | Пример |
| :--- | :--- | :--- |
| `published_at_utc` | Дата и время публикации (UTC) | `2026-03-30 12:00:15` |
| `genre` | Категория / Жанр релиза | `ambient` |
| `artist` | Исполнитель / Лейбл | `Joe Fujinoki` |
| `album_title` | Название альбома/трека | `Glass Torso` |
| `url` | Прямая ссылка на Bandcamp | `https://joefujinoki.bandcamp.com/album/glass-torso` |
| `tags` | Теги релиза | `ambient, drone` |
| `image_url` | Прямая ссылка на обложку | `https://f4.bcbits.com/img/a1234567890_10.jpg` |

---

## Лицензия

Проект распространяется под лицензией MIT.
