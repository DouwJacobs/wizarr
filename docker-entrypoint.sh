#!/usr/bin/env sh
set -eu

echo "[entrypoint] 🚀 Starting Wizarr container…"

# ───────── 1) Create or reuse the chosen UID/GID ──────────
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if [ "$(id -u)" = "0" ]; then
  echo "[entrypoint] 👤 Wanted UID=$PUID  GID=$PGID"

  # Figure out which *names* already map to those numeric IDs
  EXISTING_USER="$(getent passwd "$PUID"  | cut -d: -f1 || true)"
  EXISTING_GRP="$(getent group  "$PGID"  | cut -d: -f1 || true)"

  # Decide what account we’ll run as
  TARGET_USER="${EXISTING_USER:-wizarruser}"
  TARGET_GRP="${EXISTING_GRP:-wizarrgroup}"

  # Create group only if the GID isn’t taken
  if [ -z "$EXISTING_GRP" ]; then
    addgroup -S -g "$PGID" "$TARGET_GRP"
  fi

  # Create user only if the UID isn’t taken
  if [ -z "$EXISTING_USER" ]; then
    adduser  -S -G "$TARGET_GRP" -u "$PUID" "$TARGET_USER"
  else
    # Make sure the existing user is in the right group
    adduser "$EXISTING_USER" "$TARGET_GRP" || true
  fi

  # Fix ownership of bind-mounts
  chown -R "$TARGET_USER":"$TARGET_GRP" /data /.cache /opt/default_wizard_steps

  # Re-exec as that user
  exec su-exec "$TARGET_USER":"$TARGET_GRP" "$0" "$@"
fi

echo "[entrypoint] 👍 Running as $(id -un):$(id -gn) ($(id -u):$(id -g))"

# ───────── The rest of your original script (seed DB etc.) ─────────
# …
