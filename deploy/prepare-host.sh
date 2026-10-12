#!/bin/sh
# Run on the selected Linux VPS with sudo. This only prepares this app's directories.
set -eu

if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' 'Run with sudo: sudo sh deploy/prepare-host.sh' >&2
    exit 1
fi

command -v docker >/dev/null 2>&1 || {
    printf '%s\n' 'Install Docker Engine and the Compose plugin before running this script.' >&2
    exit 1
}
docker compose version >/dev/null

tg_uid=${TG_RUN_UID:-10001}
tg_gid=${TG_RUN_GID:-10001}
case "$tg_uid" in
    '' | *[!0-9]*) printf '%s\n' 'TG_RUN_UID must be a number.' >&2; exit 1 ;;
esac
case "$tg_gid" in
    '' | *[!0-9]*) printf '%s\n' 'TG_RUN_GID must be a number.' >&2; exit 1 ;;
esac
if [ "$tg_uid" -eq 0 ] || [ "$tg_gid" -eq 0 ]; then
    printf '%s\n' 'The application must run as a non-root user and group.' >&2
    exit 1
fi

if getent group telegram-mcp >/dev/null 2>&1; then
    actual_gid=$(getent group telegram-mcp | cut -d: -f3)
    [ "$actual_gid" = "$tg_gid" ] || {
        printf '%s\n' 'Existing telegram-mcp group has another GID; use its GID explicitly.' >&2
        exit 1
    }
else
    if getent group "$tg_gid" >/dev/null 2>&1; then
        printf '%s\n' 'GID already belongs to another group; select a free TG_RUN_GID.' >&2
        exit 1
    fi
    groupadd --system --gid "$tg_gid" telegram-mcp
fi

if id telegram-mcp >/dev/null 2>&1; then
    [ "$(id -u telegram-mcp)" = "$tg_uid" ] && [ "$(id -g telegram-mcp)" = "$tg_gid" ] || {
        printf '%s\n' 'Existing telegram-mcp account has different IDs; use its IDs explicitly.' >&2
        exit 1
    }
else
    if getent passwd "$tg_uid" >/dev/null 2>&1; then
        printf '%s\n' 'UID already belongs to another account; select a free TG_RUN_UID.' >&2
        exit 1
    fi
    useradd --system --uid "$tg_uid" --gid "$tg_gid" \
        --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin telegram-mcp
fi

install -d -o root -g root -m 0755 /opt/telegram-personal-mcp
install -d -o root -g root -m 0755 /opt/telegram-personal-mcp/app
install -d -o "$tg_uid" -g "$tg_gid" -m 0700 /opt/telegram-personal-mcp/data
install -d -o root -g root -m 0700 /opt/telegram-personal-mcp/secrets

printf '%s\n' 'Dedicated directories are ready; no service was started or modified.'
printf 'Container IDs: %s:%s\n' "$tg_uid" "$tg_gid"
printf '%s\n' 'Install telegram.json with owner telegram-mcp:telegram-mcp and mode 0600.'
