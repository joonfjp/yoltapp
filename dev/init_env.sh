#!/bin/sh
set -eu
umask 077

if [ ! -e .env ]; then
    port=${YOLT_DB_PORT:-5489}
    sed "s/^YOLT_DB_PORT=5489\$/YOLT_DB_PORT=$port/" .example.env > .env
fi

unset YOLT_DATABASE_URL
. ./.env
if [ -e .db-password ]; then
    if [ ! -f .db-password ] || [ ! -s .db-password ]; then
        echo "Expected a nonempty password file at .db-password. Restore it before starting Yolt." >&2
        exit 1
    fi
elif [ -n "${YOLT_DATABASE_URL:-}" ]; then
    echo "Missing .db-password. Restore the password file matching YOLT_DATABASE_URL before starting Yolt." >&2
    exit 1
else
    password=$(openssl rand -hex 24)
    (set -C; printf '%s' "$password" > .db-password)
fi

if [ -z "${YOLT_DATABASE_URL:-}" ]; then
    # The password generator and rotation script both write hexadecimal passwords.
    password=$(cat .db-password)
    case "$password" in
        *[!0-9a-fA-F]*)
            echo "Set YOLT_DATABASE_URL in .env to match your custom .db-password." >&2
            exit 1 ;;
    esac
    port=${YOLT_DB_PORT:-5489}
    chmod 600 .env
    printf '\nYOLT_DATABASE_URL=postgresql://yolt:%s@127.0.0.1:%s/yolt\n' "$password" "$port" >> .env
    echo "Configured YOLT_DATABASE_URL in .env using .db-password."
fi
