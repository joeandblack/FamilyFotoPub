#!/bin/bash
# Clean-Mac qualification checks for the FamilyFoto Mac installer. Run inside a disposable
# Apple Silicon macOS VM, never on a machine with a live library. See QUALIFICATION.md.
#
#   bash qualify.sh clean                      the VM has no Homebrew, Command Line Tools or Python
#   bash qualify.sh offline                    networking is really off (for the offline run)
#   bash qualify.sh app "/Volumes/FamilyFoto Setup/FamilyFoto Setup.app"   launcher self-check
#   bash qualify.sh installed ~/FamilyFoto     an installation made by the launcher
#
# Uses only macOS's own tools (/bin/bash, /usr/bin/*), so it runs before anything is installed.
set -uo pipefail
PASS=0 FAIL=0
ok() { printf 'PASS  %s\n' "$*"; PASS=$((PASS + 1)); }
bad() { printf 'FAIL  %s\n' "$*"; FAIL=$((FAIL + 1)); }
check() { local what="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$what"; else bad "$what"; fi; }
finish() { printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"; [[ $FAIL -eq 0 ]]; exit $?; }

clean() {
    [[ "$(uname -m)" == arm64 ]] && ok "Apple Silicon" || bad "Apple Silicon (found $(uname -m))"
    local version; version="$(sw_vers -productVersion)"
    [[ "${version%%.*}" -ge 27 ]] && ok "macOS $version (27 or newer)" || bad "macOS $version is older than 27, which FamilyFoto requires"
    [[ ! -e /opt/homebrew && ! -e /usr/local/Homebrew ]] && ok "no Homebrew" || bad "Homebrew is present"
    # xcode-select -p only reports; it never offers to install anything.
    xcode-select -p >/dev/null 2>&1 && bad "Command Line Tools are installed ($(xcode-select -p))" || ok "no Command Line Tools"
    [[ ! -x /Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 ]] && ok "no python.org Python 3.12" || bad "python.org Python 3.12 is installed"
    [[ "$(id -u)" != 0 ]] && ok "signed in as a regular user" || bad "running as root"
    # Some VM images ship with Gatekeeper off, which hides the Open Anyway step users will see.
    local gatekeeper; gatekeeper="$(/usr/sbin/spctl --status 2>&1)"
    [[ "$gatekeeper" == *"assessments enabled"* ]] && ok "Gatekeeper is on" || bad "Gatekeeper is off ($gatekeeper)"
    finish
}

offline() {
    if /usr/bin/curl -sS --max-time 8 -o /dev/null https://github.com 2>/dev/null; then bad "network is still reachable (github.com)"; else ok "github.com unreachable"; fi
    if /usr/bin/curl -sS --max-time 8 -o /dev/null https://pypi.org 2>/dev/null; then bad "network is still reachable (pypi.org)"; else ok "pypi.org unreachable"; fi
    finish
}

app() {
    local bundle="$1" exe="$1/Contents/MacOS/FamilyFoto Setup"
    check "app is ad-hoc signed and intact" /usr/bin/codesign --verify --strict "$bundle"
    local signature; signature="$(/usr/bin/codesign -dv "$bundle" 2>&1)"
    [[ "$signature" == *Signature=adhoc* ]] && ok "signature is ad-hoc (expected: not notarized)" || bad "unexpected signature"
    if out="$("$exe" --check 2>&1)"; then ok "launcher --check: ${out%%$'\n'*}"; else bad "launcher --check: $out"; fi
    local cache; cache="$(ls -d "$HOME"/Library/Application\ Support/FamilyFoto/Setup/* 2>/dev/null | head -1)"
    local cached; cached="$(/usr/bin/xattr -l "$cache"/*.zip 2>/dev/null)"
    [[ -n "$cache" && "$cached" != *com.apple.quarantine* ]] \
        && ok "bootstrap cache carries no quarantine mark" || bad "bootstrap cache missing or quarantined"
    finish
}

installed() {
    local root="$1" b="$1/bundled"
    for exe in python/bin/python3.12 postgres/bin/initdb postgres/bin/pg_ctl postgres/bin/postgres ffmpeg/bin/ffmpeg ffmpeg/bin/ffprobe setup-window/bin/familyfoto-setup; do
        [[ -x "$b/$exe" ]] && ok "bundled $exe" || bad "bundled $exe missing"
    done
    local marks; marks="$(/usr/bin/xattr -r -l "$root" 2>/dev/null)"
    [[ "$marks" != *com.apple.quarantine* ]] && ok "no quarantine marks in the installation" || bad "quarantine marks found in the installation"
    local receipt; receipt="$("$b/python/bin/python3.12" -I -B "$root/installer/macos/bundled.py" resume-receipt "$root" 2>&1)" \
        && ok "resume record valid ($receipt)" || bad "resume record: $receipt"
    # Every non-system library must come from the installation, never from /opt/homebrew or /usr/local.
    local outside=0
    for exe in "$b"/python/bin/python3.12 "$b"/ffmpeg/bin/ffmpeg "$b"/postgres/Cellar/*/*/bin/postgres "$b"/postgres/Cellar/*/*/bin/initdb; do
        local loaded; loaded="$(DYLD_PRINT_LIBRARIES=1 "$exe" --version 2>&1 >/dev/null)"
        [[ "$loaded" == *"> /opt/homebrew"* || "$loaded" == *"> /usr/local"* ]] && outside=1
    done
    [[ $outside == 0 ]] && ok "bundled programs load only macOS and installation libraries" || bad "a bundled program loaded a library from outside"
    if [[ -f "$root/runtime/macos-database-ready.json" ]]; then
        local port; port="$(/usr/bin/plutil -extract web_port raw -o - "$root/runtime/macos-preparation.json" 2>/dev/null || echo 4341)"
        /usr/bin/curl -sk --max-time 5 "https://127.0.0.1:$port/api/macos-health" >/dev/null && ok "FamilyFoto answers on https://127.0.0.1:$port" \
            || bad "FamilyFoto does not answer on port $port (start it with FamilyFoto.app first)"
        local agents; agents="$(/bin/launchctl print "gui/$(id -u)" 2>/dev/null)"
        [[ "$agents" == *local.familyfoto* ]] && ok "per-user launchd agent registered" || bad "launchd agent not registered"
    else
        bad "setup has not finished (runtime/macos-database-ready.json missing)"
    fi
    finish
}

case "${1:-}" in
    clean) clean ;;
    offline) offline ;;
    app) [[ $# -eq 2 ]] || { echo "usage: qualify.sh app PATH_TO_APP"; exit 2; }; app "$2" ;;
    installed) [[ $# -eq 2 ]] || { echo "usage: qualify.sh installed INSTALLATION_FOLDER"; exit 2; }; installed "$2" ;;
    *) sed -n '2,12p' "$0"; exit 2 ;;
esac
