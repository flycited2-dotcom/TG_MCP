# Развёртывание TG MCP на Linux VPS

Инструкция предназначена для собственного сервера и одного владельца Telegram.
Все домены и пути credentials ниже — шаблоны. Секреты создаются отдельно от Git.

## 1. Подготовка

Установите Docker Engine, Compose plugin и Nginx по инструкциям поставщиков.
Настройте DNS своего домена и действующий HTTPS-сертификат.

```sh
git clone https://github.com/flycited2-dotcom/TG_MCP.git
cd TG_MCP
sudo sh deploy/prepare-host.sh
sudo cp -a . /opt/telegram-personal-mcp/app/
```

`prepare-host.sh` проверяет Docker и создаёт отдельный системный аккаунт с
UID/GID 10001 и каталоги `app`, `data`, `secrets`. Если IDs заняты, задайте
свободные `TG_RUN_UID` и `TG_RUN_GID` для скрипта и Compose. Сам скрипт не
устанавливает Docker и не запускает сервисы.

## 2. Закрытые файлы

Создайте `/opt/telegram-personal-mcp/secrets/telegram.json` с полями
`api_id` (число), `api_hash` и `session` (авторизованная Telethon StringSession).
API-учётные данные получают через [my.telegram.org](https://my.telegram.org).
Авторизация Telegram выполняется отдельно; Bot API token не заменяет session.
Не отправляйте session, код входа или пароль Telegram в чат AI-клиента.

```sh
sudo chown 10001:10001 /opt/telegram-personal-mcp/secrets/telegram.json
sudo chmod 0600 /opt/telegram-personal-mcp/secrets/telegram.json
```

Конфигурация владельца OAuth — отдельный пароль подключения, а не пароль
Telegram. Следующий скрипт спрашивает его локально и записывает только соль
и hash. Выполняйте один раз при установке: он откажется перезаписывать файл.

```sh
sudo python3 - <<'PY'
import base64, getpass, hashlib, json, os, secrets
path = '/opt/telegram-personal-mcp/data/auth.json'
password = getpass.getpass('New MCP owner password: ')
if len(password) < 16:
    raise SystemExit('Use a strong password of at least 16 characters')
if password != getpass.getpass('Repeat MCP owner password: '):
    raise SystemExit('Passwords differ')
salt = secrets.token_bytes(32)
digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=64)
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as stream:
    json.dump({'password_salt': base64.b64encode(salt).decode(),
               'password_hash': base64.b64encode(digest).decode()}, stream)
os.chown(path, 10001, 10001)
PY
```

При других UID/GID замените оба значения `10001`. Не запускайте инициализацию
повторно при обновлении приложения: существующие credentials и базы сохраняются.

## 3. Compose и сеть

```sh
cd /opt/telegram-personal-mcp/app
cp deploy/.env.example deploy/.env
# Edit TG_PUBLIC_URL, paths and UID/GID in deploy/.env.
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d --build
```

Compose использует Linux `network_mode: host`, а HTTP слушает исключительно
`127.0.0.1:8899`. Снаружи доступ предоставляет Nginx через HTTPS/OAuth.
Для session, привязанной к IPv6 endpoint Telegram, у VPS должен быть рабочий
маршрут IPv6. Сначала проверьте сеть своего сервера; StringSession не обеспечивает
доступность Telegram автоматически. Для IPv4 session нужен доступный IPv4.

В Compose сохранены запуск без root, `read_only`, `cap_drop: ALL`,
`no-new-privileges` и отдельные bind mounts. Credentials монтируются read-only.
Для включения внешних изменений задайте `TG_ALLOW_SEND=true`; соответствующий
OAuth scope всё равно обязателен.

## 4. Nginx

Включите содержимое `deploy/telegram-mcp.nginx.conf` внутрь HTTPS `server` блока
своего домена. Оно добавляет отдельный prefix `/telegram-personal-mcp/`, удаляет
его при proxy_pass и содержит discovery paths для OAuth. Не заменяйте этим
файлом весь конфиг уже работающего сайта.

```sh
sudo nginx -t
sudo systemctl reload nginx
```

Перезагружайте Nginx только после успешного `nginx -t`. Шаблон отключает access
log для endpoint, чтобы одноразовые download tickets и OAuth codes не попадали
в журналы. Публичный URL должен точно совпадать с `TG_PUBLIC_URL`.

## 5. Проверка и подключение

```sh
curl --fail http://127.0.0.1:8899/healthz
curl --fail https://YOUR_DOMAIN/telegram-personal-mcp/healthz
curl -i https://YOUR_DOMAIN/telegram-personal-mcp/mcp
```

Первые два запроса должны пройти; третий без bearer token должен вернуть
`401 authentication_required`. Затем добавьте HTTPS URL с `/mcp` в совместимом
клиенте через OAuth, подтвердите нужные scopes и вызовите `telegram_status`.
Расположение настройки и поддержка собственных MCP Apps зависят от клиента и
аккаунта. Проверяйте файлы и отображение отдельно на компьютере и телефоне.

Для Whisper подготовьте локальную модель в каталоге data и настройте
`TG_WHISPER_MODEL`; в Compose указан `/data/models/whisper-small`.
Модель и её веса не входят в Git.

## Обновление и резервные копии

Обновляйте исходники, собирайте новый контейнер и проверяйте health/status.
Сохраняйте закрытые `data/` и `secrets/` отдельно: там Telegram session,
OAuth state, результаты действий и временные файлы. Не включайте эти каталоги
в коммиты, публичные архивы или Docker build context. Восстановление приложения
из Git без этих данных не восстанавливает подключённый Telegram-аккаунт.
