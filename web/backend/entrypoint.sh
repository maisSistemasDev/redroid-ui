#!/bin/sh
set -e

if [ -n "${DATABASE_URL:-}" ]; then
  DB_WAIT_TIMEOUT="${DB_WAIT_TIMEOUT:-90}"
  echo "Waiting for database (timeout ${DB_WAIT_TIMEOUT}s)..."
  if ! python - <<'PY'
import os
import time
from urllib.parse import urlparse

import psycopg

url = os.environ.get("DATABASE_URL", "")
timeout = int(os.environ.get("DB_WAIT_TIMEOUT", "90"))
if not url:
    raise SystemExit(0)

parsed = urlparse(url)
host = parsed.hostname or "db"
port = parsed.port or 5432

deadline = time.time() + timeout
last_err = None
while time.time() < deadline:
    try:
        with psycopg.connect(url, connect_timeout=3):
            print(f"Database ready at {host}:{port}")
            raise SystemExit(0)
    except Exception as exc:
        last_err = exc
        print(f"Waiting DB {host}:{port} ... {exc}")
        time.sleep(2)

print(f"Database not ready after {timeout}s: {last_err}")
raise SystemExit(1)
PY
  then
    echo "Database connection timeout."
    exit 1
  fi
fi

if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
  echo "Fixing renamed migration records..."
  python - <<'PY'
import os, django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()
from django.db import connection
renames = [
    ("instances", "0005_vpn_gluetun_fields", "0005_vpn_extra_fields"),
    ("instances", "0006_merge_0005_vpn_gluetun_fields_0005_vpn_type_provider", "0006_merge_vpn_fields"),
]
try:
    with connection.cursor() as c:
        for app, old, new in renames:
            c.execute(
                "UPDATE django_migrations SET name=%s WHERE app=%s AND name=%s",
                [new, app, old],
            )
            if c.rowcount:
                print(f"  Renamed {app}.{old} -> {new}")
except Exception as e:
    print(f"  Migration rename skipped (fresh db?): {e}")
PY
  echo "Running migrations..."
  python manage.py migrate --noinput
fi

# Pre-pull busybox for lightweight data cleanup containers.
docker pull busybox:latest >/dev/null 2>&1 &

if [ "$#" -gt 0 ]; then
  echo "Starting custom command: $*"
  exec "$@"
fi

echo "Starting Daphne..."
exec daphne -b 0.0.0.0 -p 8000 config.asgi:application
