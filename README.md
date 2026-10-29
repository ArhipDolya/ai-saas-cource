# ai-saas-cource

Мінімальний Telegram-бот на aiogram 3.x. Команда `/start` відповідає: «Привіт! Бот працює 👋».

## Вимоги для Docker

- Docker із Docker Compose (наприклад, запущений Docker Desktop).
- Токен Telegram-бота від [BotFather](https://t.me/BotFather).
- Доступ до інтернету.

Встановлювати Python на комп’ютер для запуску через Docker не потрібно.

## Структура

```text
app/main.py       # Запуск бота й обробник /start
requirements.txt  # Python-залежності
.env              # Локальний BOT_TOKEN, виключено з Git
.env.example      # Приклад налаштувань без секретів
README.md         # Інструкція запуску
Dockerfile        # Образ із Python, залежностями та кодом
docker-compose.yml # Запуск із hot reload для розробки
.dockerignore     # Виключення секретів і локальних файлів з контексту збірки
```

## Запуск через Docker

Якщо `.env` ще немає, створіть його з прикладу:

```bash
cp .env.example .env
```

У `.env` встановіть власний `BOT_TOKEN`. Перед запуском зупиніть локальний процес бота через `Ctrl+C`, якщо він працює: два процеси polling з одним токеном конфліктують.

З кореня проєкту виконайте:

```bash
docker compose up --build
```

Команда збере образ, встановить залежності й запустить бота. Логи з’являться в цьому ж терміналі. Надішліть `/start` своєму боту в Telegram.

Щоб зупинити бота, натисніть `Ctrl+C`. Щоб також видалити контейнер і мережу цього Compose-проєкту:

```bash
docker compose down
```

## Hot reload

Під час роботи `docker compose up --build` змінюйте та зберігайте Python-файли в `app/`. Compose монтує локальну папку в контейнер, а `watchfiles` автоматично перезапускає процес бота після змін. Сам контейнер і образ при цьому залишаються тими самими.

Для перевірки змініть `START_GREETING` у `app/main.py`, збережіть файл і дочекайтеся повідомлення про перезапуск у логах. Надішліть `/start` ще раз: бот має відповісти новим текстом. Зміни в підпапках `app/` також відстежуються.

`WATCHFILES_FORCE_POLLING=true` вмикає періодичну перевірку файлів, щоб зміни в змонтованій папці на Docker Desktop виявлялися надійно. Це додає невелике навантаження; відстежується лише `app/`.

Для інших змін:

| Що змінилося | Що виконати |
| --- | --- |
| Python-код у `app/` | Просто зберегти файл |
| `requirements.txt` або `Dockerfile` | `docker compose up --build` після зупинки поточного запуску |
| `.env` | `docker compose up -d --force-recreate bot` |
| `docker-compose.yml` | Зупинити поточний запуск і виконати `docker compose up --build` |

Звичайний `docker compose restart` не перечитує `.env`: для нового значення потрібне перестворення контейнера. `.env` передається через `env_file` під час запуску й не копіюється в образ.

Цей режим hot reload призначений для розробки.

## Локальний запуск без Docker

Потрібен Python 3.10 або новіший. Не запускайте локального бота одночасно з контейнером.

Виконайте з кореня проєкту (macOS / Linux):

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Якщо `.env` ще немає, створіть його з прикладу:

```bash
cp .env.example .env
```

У `.env` замініть `your_bot_token_here` на власний токен. Не додавайте справжній токен у код, README або `.env.example`.

```bash
python -m app.main
```

Бот автоматично читає `.env` з кореня проєкту. Уже встановлений `BOT_TOKEN` у змінних середовища має пріоритет над `.env`.

## Перевірка

1. Залиште процес запущеним у терміналі.
2. Відкрийте свого бота в Telegram і надішліть `/start`.
3. Переконайтеся, що бот відповідає: «Привіт! Бот працює 👋».
4. Натисніть `Ctrl+C` у терміналі, щоб зупинити бота.

Бот використовує long polling: він сам отримує оновлення від Telegram. Для запуску не потрібні nginx, ngrok або відкриті порти. Запускайте лише один процес polling для одного токена.

Якщо `BOT_TOKEN` відсутній або порожній, застосунок завершиться з поясненням. Якщо Telegram відхиляє авторизацію, перевірте токен у BotFather. Токен, оприлюднений у чаті або репозиторії, перевипустіть через BotFather та замініть у `.env`.

## Документація

- [Приклад запуску aiogram](https://docs.aiogram.dev/en/latest/)
- [Завантаження налаштувань через python-dotenv](https://github.com/theskumar/python-dotenv)
- [Bind mounts у Docker](https://docs.docker.com/engine/storage/bind-mounts/)
- [Запуск і перезапуск команд через watchfiles](https://watchfiles.helpmanual.io/cli/)
