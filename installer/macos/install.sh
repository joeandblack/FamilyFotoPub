#!/bin/bash
set -euo pipefail
umask 077
ROOT="$(cd "$(dirname "$0")/../.." && pwd -P)"
MODE="${1:-plan}"
# The shared installer bundles Python 3.12; otherwise Homebrew provides it, as before.
BUNDLED_PYTHON="$ROOT/bundled/python/bin/python3.12"
# A release installation resumed from a fresh shell: find its verified local inputs again.
if [[ -z "${FAMILYFOTO_PAYLOAD_RECEIPT:-}" && -f "$ROOT/bundled/receipt.json" && "$MODE" != upgrade ]]; then
    if [[ ! -x "$BUNDLED_PYTHON" ]]; then
        printf 'The bundled Python is missing; run setup again from the FamilyFoto launcher.\n' >&2; exit 1
    fi
    FAMILYFOTO_PAYLOAD_RECEIPT="$("$BUNDLED_PYTHON" -I -B "$ROOT/installer/macos/bundled.py" resume-receipt "$ROOT")" || exit 1
    export FAMILYFOTO_PAYLOAD_RECEIPT
fi
if [[ $# -gt 0 ]]; then shift; fi
case "$MODE" in
upgrade)
    if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
        printf 'Application Python is missing; no changes made.\n' >&2; exit 1
    fi
    exec "$ROOT/.venv/bin/python" -B "$ROOT/installer/upgrade-catalog.py" \
        --config-root "$ROOT/config" --data-root "$ROOT/data" "$@";;
plan)
    printf 'FamilyFoto macOS installation plan\nApplication: %s\n' "$ROOT"
    if [[ -n "${FAMILYFOTO_PAYLOAD_RECEIPT:-}" ]]; then
        printf '%s\n' 'Target: Apple Silicon, Python 3.12, PostgreSQL 17, FFmpeg; bundled by this release (no Homebrew).'
    else
        printf '%s\n' 'Target: Apple Silicon, Python 3.12, PostgreSQL 17, FFmpeg; existing Homebrew required.'
    fi
    printf '%s\n' \
      'prepare: dependencies, private app environment, credentials; no services started.' \
      'install: Prepare plus private database and local launchers; leaves services stopped.' \
      'install: asks whether to install People: isolated InsightFace CPU (non-commercial research use only)' \
      '  and an empty review-only catalog. Unattended installs choose with --with-people --research-only' \
      '  or --without-people.' \
      'install asks New library or Restore from a backup. restore: Core, then the backup restored into this' \
      '  installation in one go (--backup, --media in-place|copy, --media-folder); run again to carry on.' \
      'Default ports: database 5433, web 4341, People 54341; install accepts separate test ports.' \
      'Fresh code-only copy required. Storage is selected during first import.' \
      'Plan is read-only. Release, reboot and physical disk-interruption qualification remain separate.'
    if [[ -n "${FAMILYFOTO_PAYLOAD_RECEIPT:-}" && -x "$BUNDLED_PYTHON" ]]; then
        exec "$BUNDLED_PYTHON" -B "$ROOT/installer/macos/plan.py" "$@"
    fi
    if [[ -x /opt/homebrew/opt/python@3.12/bin/python3.12 ]]; then
        exec /opt/homebrew/opt/python@3.12/bin/python3.12 -B "$ROOT/installer/macos/plan.py" "$@"
    fi
    printf '%s\n' 'Detailed checks unavailable: install Homebrew python@3.12 explicitly first.'
    if [[ $# -gt 0 ]]; then printf 'Options could not be validated without Python 3.12.\n' >&2; exit 2; fi
    exit 0;;
prepare|install|restore) ;;
*) printf 'Usage: bash installer/macos/install.sh [plan|prepare|install|restore|upgrade] [options]\n' >&2; exit 2;;
esac
if [[ "$(uname -s)" != Darwin || "$(uname -m)" != arm64 || "$EUID" == 0 ]]; then
    printf 'Requires native Apple Silicon, signed-in user (not sudo).\n' >&2; exit 1
fi
if [[ -n "${FAMILYFOTO_PAYLOAD_RECEIPT:-}" ]]; then
    PYTHON="$BUNDLED_PYTHON"
    if [[ ! -x "$PYTHON" ]]; then
        printf 'The bundled Python is missing; run setup again from the FamilyFoto launcher.\n' >&2; exit 1
    fi
else
    PYTHON=/opt/homebrew/opt/python@3.12/bin/python3.12
    if [[ ! -x /opt/homebrew/bin/brew ]]; then
        printf 'Install Homebrew explicitly from https://brew.sh first.\n' >&2; exit 1
    fi
    if [[ ! -x "$PYTHON" ]]; then
        printf 'Install Homebrew python@3.12 first; no global Python bootstrap is performed.\n' >&2; exit 1
    fi
fi
exec "$PYTHON" -B "$ROOT/installer/macos/setup_bootstrap.py" "$MODE" "$@"
