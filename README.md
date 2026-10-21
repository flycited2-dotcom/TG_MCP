# TG MCP — личный Telegram для AI-клиента

MCP-сервер для работы с собственным аккаунтом Telegram через Telethon/MTProto.
Поддерживает Streamable HTTP, OAuth с PKCE и MCP Apps для показа оригинальных
фотографий и предпросмотра статьи в совместимом клиенте, включая ChatGPT.

Текущая версия сервера: **2.3.0**, **108 инструментов**.

## Возможности

- Поиск доступных чатов, история с пагинацией, поиск сообщений и «Избранное».
- Загрузка файлов из клиента через `openai/fileParams`, получение вложений из
  Telegram, временные ссылки на оригиналы и проверка размера/SHA-256.
- Предпросмотр оригинальных изображений в MCP Apps. Формат определяется по
  байтам; исходные файлы сохраняются без подмены изображениями из интернета.
- Подготовка и отправка текста, фото, альбомов, документов, аудио и видео;
  ответы, пересылка, правка, удаление, реакции и закрепление.
- Одна нативная статья Telegram Rich Message: заголовки, абзацы, ссылки,
  списки и фотографии между текстовыми блоками.
- Stories, операции с контактами, группами, каналами и темами — в пределах
  прав подключённого аккаунта и выданных OAuth scopes.
- Чтение документов и таблиц, локальная транскрибация Whisper, WAV-превью
  и кадры видео. Для части обработчиков нужны системные программы.
- Работа с ботами как у человека в приложении: клавиатуры под сообщениями и
  внизу чата, нажатие кнопок, ожидание ответа бота, команды, голосовые,
  inline-режим, права ботов в каналах и группах, публикация и правка постов
  канала, изменение собственных ботов через @BotFather (см. ниже).

### Боты

| Задача | Инструменты |
|---|---|
| Увидеть кнопки | `telegram_get_keyboard`, `telegram_get_reply_keyboard`, поле `keyboard` и `edited_at` в `telegram_read_messages` |
| Нажать кнопку | `telegram_prepare_press_button` → `telegram_execute_action` (параметр `wait_reply_s` возвращает ответ бота) |
| Дождаться ответа | `telegram_wait_for_bot_reply` (новые и изменённые сообщения, до 110 с) |
| О боте | `telegram_get_bot_info` (команды, кнопка меню, inline) |
| Голос, команды, ответ на сообщение | `telegram_prepare_voice`, `telegram_prepare_send` (пометка `bot_command`, `reply_to_message`), `telegram_forward` с `drop_author` |
| Inline-режим | `telegram_inline_query` → `telegram_prepare_send_inline_result` |
| Права ботов (`telegram.manage`) | `telegram_list_admins`, `telegram_prepare_add_bot_admin`, `…_edit_bot_rights`, `…_demote_bot`, `…_remove_bot`, `…_set_bot_title` |
| Посты каналов | `telegram_prepare_publish_post` (`telegram.send`); правка, замена медиа, удаление, закрепление (`telegram.manage`); `telegram_get_post_stats` |
| Свои боты | `telegram_prepare_botfather_change` (выключено, пока владелец не задал `TG_ALLOW_BOTFATHER=true`) |

Правила безопасности. Всё, что действует на бота или чат, проходит
prepare → предпросмотр → execute. Кнопки адресуются стабильным `button_ref`;
если бот перерисовал клавиатуру, нажатие не выполняется (`stale_button`).
Данные callback-кнопок агенту не отдаются. Никогда не нажимаются кнопки
телефона, геолокации, опроса, оплаты, игры и входа на сторонний сайт; ссылки и
web app только возвращаются как данные. Ответы ботов и подписи кнопок считаются
недоверенными данными. `add_admins` ботам не выдаётся. Права людей не
меняются. Токены в ответах @BotFather заменяются на `[token redacted]`, команды
`/token`, `/revoke`, `/deletebot` невозможны. Каждое действие попадает в журнал
аудита (клиент, действие, чат, результат, без текстов).

### Группы, папки и осторожная рассылка

| Задача | Инструменты |
|---|---|
| Свои чаты | `telegram_list_chats` (по названию, типу, папке) |
| Найти новые группы | `telegram_search_public_chats`, затем `telegram_inspect_chat` (размер, правила в закрепе, режим вступления, медленный режим, права писать) |
| Вступить / подать заявку | `telegram_prepare_join_chats` (до 10 за одно утверждение; @username, ссылка, инвайт) |
| Выйти с удалением истории | `telegram_prepare_leave_chats` (владельцем чат не покидается; чат убирается и из папок) |
| Папки Telegram | `telegram_list_folders`, `telegram_get_folder`, `telegram_prepare_folder_change` (создать, переименовать, удалить, добавить и убрать чаты) |
| Рассылка | `telegram_prepare_campaign` по папке или списку чатов; ход смотреть в `telegram_job_status`, остановить `telegram_job_cancel`, лимиты в `telegram_limits_status` |

Вступления, выходы и рассылка выполняются фоновой задачей: по одному шагу со
случайными паузами. Утверждается весь список и точный текст один раз. Пропускаются
чаты, где писать нельзя, и чаты, где тот же текст уже отправлялся недавно.
Любое предупреждение Telegram (flood, спам) останавливает задачу целиком; шаг с
неизвестным исходом не повторяется. Лимиты задаёт только владелец сервера:
`TG_JOIN_DAILY_LIMIT` (10), `TG_LEAVE_DAILY_LIMIT` (30), `TG_CAMPAIGN_DAILY_LIMIT`
(20), `TG_CAMPAIGN_MAX_CHATS` (30), `TG_CAMPAIGN_MIN_DELAY` (45 секунд). Реклама во
многие группы с личного аккаунта может привести к ограничениям: используйте
короткие списки и только там, где правила группы это разрешают.

### Остальной функционал и чего намеренно нет

| Задача | Инструменты |
|---|---|
| Настройки списка чатов | `telegram_prepare_chat_setting`: звук (mute/unmute), архив, закрепление чата, «прочитано» |
| Удалить историю личных чатов и ботов | `telegram_prepare_delete_chats` (группы и каналы: `telegram_prepare_leave_chats`) |
| Поиск по всем своим чатам | `telegram_search_all_messages` |
| Отложенные сообщения | `telegram_get_scheduled`, `telegram_prepare_delete_scheduled` |
| Опросы и викторины | `telegram_prepare_poll` |
| Свои группы и каналы | `telegram_prepare_chat_admin_setting` (адрес, медленный режим, права участников, фото, одобрение вступления), `telegram_prepare_topic_change`, `telegram_get_admin_log` |
| Пригласительные ссылки и заявки | `telegram_list_invite_links`, `telegram_prepare_invite_link`, `telegram_list_join_requests`, `telegram_prepare_join_requests_decision` |
| Удалить свой чат для всех | `telegram_prepare_delete_chat_for_all` (нужно точное название чата) |
| Блокировка | `telegram_prepare_block_user` |

Намеренно не реализовано: сессии и устройства, двухфакторная защита, смена
номера, коды входа, настройки приватности, платежи, подарки и звёзды, массовая
выгрузка контактов с номерами, секретные чаты и звонки, Telegram Business,
изменение собственного профиля, создание и удаление ботов.

Быстрые нажатия без второго подтверждения задаёт только владелец сервера в
файле `bot-fastpath.json` (пример: `deploy/bot-fastpath.example.json`); без файла
`auto_confirm` всегда возвращает `bot_not_allowlisted`.

Внешние изменения имеют отдельные этапы подготовки и выполнения. Клиент
должен показать получателя, содержание и порядок медиа и получить согласие
человека перед `execute_action`, `publish_article` или `publish_story`.
Сервер проверяет hash черновика, ограничивает срок его действия и хранит
результат выполнения. Неопределённый исход не запускает автоматическую отправку
повторно. Проверка hash сама по себе не доказывает согласие человека.

## Развёртывание

Нужны Python 3.12+, собственные Telegram API-учётные данные и авторизованная
Telethon StringSession. Для VPS предусмотрены Docker Compose и Nginx.

1. Клонируйте этот репозиторий.
2. Подготовьте закрытые файлы Telegram credentials и конфигурации владельца.
3. Скопируйте `deploy/.env.example` в `deploy/.env` и задайте свой HTTPS URL.
4. Настройте reverse proxy и запустите контейнер.
5. Подключите адрес `https://YOUR_DOMAIN/telegram-personal-mcp/mcp` в
   совместимом MCP-клиенте через OAuth.

Полная последовательность: [VPS_SETUP.md](VPS_SETUP.md).

### Клиенты

Один и тот же сервер подключается к ChatGPT, Claude, Claude Code и Codex.
OAuth принимает HTTPS-callback и локальные `http://localhost` / `127.0.0.1` /
`[::1]` (RFC 8252, порт может меняться); параметр `resource` необязателен.

- **Claude (claude.ai и приложения):** Настройки → Коннекторы → «Добавить свой
  коннектор», адрес `/mcp`, Client ID и secret пустые.
- **Claude Code:** `claude mcp add --transport http --scope user telegram https://YOUR_DOMAIN/telegram-personal-mcp/mcp`,
  затем `/mcp` → telegram → Authenticate.
- **Codex:** блок `[mcp_servers.telegram]` с `url` в `~/.codex/config.toml`, затем
  `codex mcp login telegram --scopes telegram.read,telegram.send`.

Без `openai/fileParams` (Claude, Codex) файлы загружаются через
`telegram_begin_upload`.

| Переменная | Назначение |
|---|---|
| `TG_PUBLIC_URL` | Публичный HTTPS base URL без `/mcp` в конце |
| `TG_CREDENTIALS_FILE` | Закрытый JSON с `api_id`, `api_hash`, `session` |
| `TG_OWNER_CONFIG` | JSON с солью и scrypt hash пароля владельца OAuth |
| `TG_DATA_DIR` | Каталог баз данных и временных файлов |
| `TG_AUTH_DB` | База OAuth, по умолчанию `/data/auth.sqlite` |
| `TG_ALLOW_SEND` | Разрешает внешние изменения; по умолчанию `false` |
| `TG_WHISPER_MODEL` | Локальный путь или имя модели Whisper |
| `TG_BOT_FASTPATH_FILE` | JSON владельца со списком ботов и действий для `auto_confirm`; без файла быстрых действий нет |
| `TG_ALLOW_BOTFATHER` | Разрешает изменение собственных ботов через @BotFather; по умолчанию `false` |
| `MCP_HOST`, `MCP_PORT` | Адрес и порт HTTP-сервера |

Scopes: `telegram.read` для чтения, `telegram.send` для отправки,
`telegram.manage` для изменения контактов и управления чатами.
Дополнительные scopes не предоставляют права администратора Telegram.

## Проверки

```sh
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
```

В наборе 129 тестов. Они используют фиктивный Telegram-клиент и временные
файлы; реальные сообщения не отправляются. GitHub Actions запускает этот
набор на Python 3.12. Тесты не заменяют проверку отображения в конкретном
клиенте, точности распознавания реальной речи и доступности методов аккаунту.

## Ограничения

- Сервер рассчитан на одного владельца; это не многопользовательский сервис.
- Доступны только чаты, сообщения и контакты, которые видит аккаунт.
  Скрытые телефоны и закрытые списки подписчиков не раскрываются.
- Rich Messages и Stories требуют поддержки установленной версии Telegram API,
  клиента и аккаунта. При ошибке статья не заменяется молча альбомом или HTML.
- MCP Apps должен поддерживаться клиентом. Объявление CSP сервером само по
  себе не подтверждает, что клиент применяет эту политику.
- Передача файлов через `openai/fileParams` зависит от клиента. Внутренний
  ID вложения без доступных байтов не считается загруженным файлом.
- Временная ссылка на оригинал действует 10 минут; черновик — 10 минут.
  По умолчанию staging ограничен 200 MiB на файл и 500 MiB суммарно.
- Для FFmpeg, LibreOffice, eSpeak и Whisper в Docker предусмотрена установка
  зависимостей; модель Whisper нужно подготовить отдельно. eSpeak не клонирует
  голос. Для сканированных PDF отдельный OCR пока не реализован.

## Состав проекта

- `server.py`, `extension_tools.py`: MCP-инструменты, HTTP и MCP Apps.
- `telegram_backend.py`, `extended_backend.py`: операции Telegram.
- `oauth_owner.py`: авторизация единственного владельца.
- `action_store.py`, `file_store.py`, `file_downloads.py`, `upload_routes.py`:
  черновики, staging и временные ссылки.
- `tool_models.py`, `media_processing.py`: схемы и обработка файлов.
- `tests/`: проверки; `deploy/`: Docker и шаблон Nginx.
- [API_ROADMAP.md](API_ROADMAP.md): предложения следующих функций.

Секреты, Telegram-сессии, пользовательские вложения, персональные отчёты и
операторские скрипты в репозиторий не входят. Не сохраняйте credentials в Git,
не публикуйте raw session strings и не открывайте порт backend в интернет.
