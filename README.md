# musicdl

Личная консольная программа: скачивает музыку в mp3 по списку треков.

- **Вход 1 (основной):** CSV-файл, выгруженный из [TuneMyMusic](https://www.tunemymusic.com/). Работает **без Spotify**.
- **Вход 2:** ссылка Spotify на трек, альбом или плейлист. **Premium и ключи не нужны.**

Файлы сохраняются как `Исполнитель - Название.mp3`, с тегами (исполнитель, название, альбом) и обложкой.

Под капотом используется [spotDL](https://github.com/spotDL/spotify-downloader) 4.5.2 (лицензия MIT). Он ищет трек на YouTube Music, скачивает его через yt-dlp, конвертирует в mp3 через ffmpeg и записывает теги.

---

## Установка на Windows (один раз)

### 1. Установите Python

1. Откройте https://www.python.org/downloads/windows/ и скачайте **Python 3.12** (подойдёт любой от 3.10 до 3.14).
2. Запустите установщик и **обязательно поставьте галочку «Add python.exe to PATH»**, затем нажмите *Install Now*.
3. Проверьте установку. Откройте меню «Пуск», введите `cmd`, нажмите Enter и выполните:
   ```bat
   py --version
   ```
   Должно появиться что-то вроде `Python 3.12.x`.

### 2. Скачайте программу

- **Вариант А (проще):** на странице репозитория на GitHub нажмите зелёную кнопку **Code → Download ZIP** и распакуйте архив, например в `C:\musicdl`.
- **Вариант Б (если установлен Git):**
  ```bat
  git clone https://github.com/newhjvf/music-download.git C:\musicdl
  ```

### 3. Создайте виртуальное окружение и установите зависимости

В окне `cmd`:

```bat
cd /d C:\musicdl
py -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -e .
```

После `activate` в начале строки появится `(.venv)`, значит окружение включено.

### 4. Установите ffmpeg (нужен для конвертации в mp3)

```bat
spotdl --download-ffmpeg
```

spotDL сам скачает ffmpeg в свою папку, отдельно ничего ставить не нужно.

Рекомендуется также поставить Deno: он помогает yt-dlp обходить защиту YouTube.

```bat
spotdl --download-deno
```

---

## Запуск

**Каждый раз** после открытия нового окна `cmd` сначала включите окружение:

```bat
cd /d C:\musicdl
.venv\Scripts\activate
```

### Скачать по CSV из TuneMyMusic

1. На tunemymusic.com выберите источник (например, свой плейлист), а в качестве назначения — **Export to file → CSV**. Сохраните файл, например в `C:\musicdl\my.csv`.
2. Сначала проверьте, что найдётся. Это пробный прогон: ничего не скачивается, только выводится таблица «трек → найденное видео + длительность»:
   ```bat
   musicdl csv my.csv --dry-run
   ```
3. Скачайте:
   ```bat
   musicdl csv my.csv
   ```

Музыка появится в папке `music`. Другую папку можно задать так: `--out "D:\Музыка"`.

### Скачать по ссылке Spotify

```bat
musicdl url "https://open.spotify.com/playlist/..."
musicdl url "https://open.spotify.com/album/..." --dry-run
musicdl url "https://open.spotify.com/track/..."
```

Ссылку берите в Spotify: «Поделиться → Копировать ссылку». Кавычки вокруг ссылки обязательны.

> **Закрытые плейлисты.** Без ключей виден только **публичный** плейлист. Закрытый либо временно сделайте публичным, либо выгрузите через TuneMyMusic в CSV.

### Полезные параметры

| Параметр | Что делает |
|---|---|
| `--dry-run` | только поиск и таблица сопоставления, без скачивания |
| `--out ПАПКА` | куда сохранять (по умолчанию `music`) |
| `--threads 4` | сколько треков качать параллельно |
| `--bitrate 320k` | качество mp3 (`128k`, `192k`, `256k`, `320k`) |
| `--report файл.csv` | куда записать список ненайденных (по умолчанию `ПАПКА\not_found.csv`) |
| `--only-verified` | брать только официальные треки YouTube Music, без клипов и любительских видео |
| `-v` | подробный лог, если что-то пошло не так |

### Что происходит при повторном запуске

- Уже скачанные файлы (`Исполнитель - Название.mp3` в папке) **пропускаются**, поэтому запуск можно повторять сколько угодно, например после ошибок сети.
- Треки, которые не нашлись или не скачались, записываются в `not_found.csv`. Этот файл открывается в Excel.

---

## Необязательно: ключи Spotify

Без них всё работает. Ключи нужны, только если хотите использовать официальный Spotify API. С февраля 2026 года Spotify требует для этого **Premium**.

1. Создайте приложение на https://developer.spotify.com/dashboard и скопируйте *Client ID* и *Client Secret*.
2. Скопируйте `.env.example` в `.env` (рядом с README) и впишите ключи:
   ```
   SPOTIFY_CLIENT_ID=ваш_id
   SPOTIFY_CLIENT_SECRET=ваш_secret
   ```
   Файл `.env` не попадает в git.

---

## Если что-то не работает

| Сообщение | Что делать |
|---|---|
| `'musicdl' is not recognized` / `не является командой` | не включено окружение: `.venv\Scripts\activate` |
| `ffmpeg is not installed` | `spotdl --download-ffmpeg` |
| много `not found`/ошибок сети, `Sign in to confirm you're not a bot` | YouTube ограничивает запросы: подождите, уменьшите `--threads 2`, включите VPN, поставьте Deno (`spotdl --download-deno`), обновите yt-dlp: `pip install -U yt-dlp` |
| найдено не то | проверьте `--dry-run`, попробуйте `--only-verified` |
| в CSV «нет колонки title/artist» | нужны колонки *Track name* и *Artist name* (как в экспорте TuneMyMusic) |

---

## Как это устроено (для разработчика)

| Задача | Чем делается |
|---|---|
| объект трека | `spotdl.types.song.Song.from_missing_data`, собирается из строки CSV (`musicdl/csv_import.py`) |
| поиск и сопоставление | `spotdl.providers.audio.ytmusic.YouTubeMusic.search` и `spotdl.utils.matching.order_results` (`musicdl/matching.py`) |
| скачивание, ffmpeg, ID3, обложка | `spotdl.download.downloader.Downloader` (`musicdl/pipeline.py`) |
| ссылки Spotify | `spotdl.utils.spotify.SpotifyClient` и `spotdl.utils.search.parse_query` (`musicdl/spotify_input.py`) |

Собственного кода немного. Вот что в нём есть сверх spotDL:

- **Разбор CSV TuneMyMusic.** Поддерживаются UTF-8 с BOM, разделители `,` `;` и табуляция, а также удаление дублей.
- **Поиск без длительности трека.** В CSV нет длительности, а spotDL отбрасывает все результаты, если длительность неизвестна. Для таких треков оценка идёт по названию, исполнителю, альбому и ISRC. Треки с известной длительностью (из Spotify) сопоставляются как в spotDL без изменений.
- **Однократный поиск.** Найденная ссылка передаётся в `Downloader`, повторного поиска нет. Обложка для треков из CSV берётся с превью YouTube.
- **Таблица `--dry-run`, пропуск уже скачанных и `not_found.csv`.** Тексты песен не скачиваются (`lyrics_providers=[]`).

Версия spotDL закреплена на `4.5.2`. В версии 4.5.0 вошёл [PR #2626](https://github.com/spotDL/spotify-downloader/pull/2626): клиент Spotify без ключей после изменений API в феврале 2026 ([issue #2617](https://github.com/spotDL/spotify-downloader/issues/2617)). 4.5.2 — последний релиз с последующими исправлениями.

Тесты (без сети, YouTube подменяется заглушками):

```bat
pip install -e .[dev]
pytest
```

Используйте программу только для музыки, которую вы вправе скачивать.
