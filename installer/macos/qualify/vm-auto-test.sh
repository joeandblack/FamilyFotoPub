#!/bin/bash
# Automated macOS 27 qualification of an offline FamilyFoto Setup DMG, run INSIDE a clean VM over SSH:
#   bash "/Volumes/My Shared Files/kit/vm-auto-test.sh" FamilyFotoSetup-<id>.dmg < test-admin-password.txt
# Covers what needs no clicking: clean VM, quarantined download, Gatekeeper, launcher self-check,
# the engine with the real bootstrap Python, unattended install with People, start/stop/start,
# installed checks, and an offline install with the network blocked. Never run on a real Mac.
set -uo pipefail
KIT="/Volumes/My Shared Files/kit"; DMG="$KIT/$1"; APP="/Applications/FamilyFoto Setup.app"
PASSWORD="$(head -1)"   # test administrator password, from stdin; never printed
step() { printf '\n== %s\n' "$*"; }
step "clean VM"; bash "$KIT/qualify.sh" clean | tail -1
rm -rf "$APP" ~/Downloads/FamilyFotoSetup.dmg ~/Library/Application\ Support/FamilyFoto ~/FamilyFoto ~/FamilyFoto-offline ~/ff-test.* ~/ff-offline.* 2>/dev/null
step "download (quarantined, as Safari leaves it) and install the app"
cp "$DMG" ~/Downloads/FamilyFotoSetup.dmg && xattr -w com.apple.quarantine "0081;$(printf %x "$(date +%s)");Safari;" ~/Downloads/FamilyFotoSetup.dmg
M=$(mktemp -d); hdiutil attach -quiet -nobrowse -readonly -mountpoint "$M" ~/Downloads/FamilyFotoSetup.dmg && ditto "$M/FamilyFoto Setup.app" "$APP" && hdiutil detach -quiet "$M"
echo "Gatekeeper before Open Anyway: $(spctl -a -t execute "$APP" 2>&1 | sed 's/.*: //')"
# macOS 27 holds a downloaded, unapproved app at launch until someone clicks Open Anyway (tested by
# hand in a second VM). Here the self-check gets 30 seconds; if Gatekeeper holds it, the bootstrap
# files are taken from the app's bundle and checked against its bootstrap.json, as the launcher does.
step "launcher self-check"
W=$(mktemp -d "$HOME/ff-test.XXXX"); mkdir -p "$W/attempt"   # no symlinks in the path (/var is one)
if out=$(timeout 30 "$APP/Contents/MacOS/FamilyFoto Setup" --check 2>&1); then
    echo "launcher --check: ${out%%$'\n'*}"; C=$(ls -d ~/Library/Application\ Support/FamilyFoto/Setup/*)
    PY="$C/python.zip"; EN="$C/engine.zip"
else
    echo "launcher held by Gatekeeper until Open Anyway (expected without a person); using the app's bundle"
    pkill -f "FamilyFoto Setup --check"
    R="$APP/Contents/Resources"; PY="$R/bundle/$(plutil -extract python.bundle_path raw "$R/bootstrap.json")"; EN="$R/bundle/$(plutil -extract engine.bundle_path raw "$R/bootstrap.json")"
    for k in python engine; do f=$([ $k = python ] && echo "$PY" || echo "$EN"); [ "$(shasum -a 256 "$f" | cut -c1-64)" = "$(plutil -extract $k.sha256 raw "$R/bootstrap.json")" ] && echo "  $k.zip matches bootstrap.json" || { echo "  $k.zip CHECKSUM MISMATCH"; exit 1; }; done
fi
step "engine with the bootstrap Python, People selected (what Install runs)"
ditto -x -k --noqtn "$PY" "$W/python"; ditto -x -k --noqtn "$EN" "$W/engine"
AG=$(ls "$W/engine/legal/"*); printf '{"root": "%s", "people": true, "agreement_digest": "%s"}' "$HOME/FamilyFoto" "$(shasum -a 256 "$AG" | cut -c1-64)" > "$W/attempt/choices.json"
start=$(date +%s)
(cd "$W/engine" && "$W/python/bin/python3.12" -E -s -B -m installer.macos.online_setup --descriptor "$APP/Contents/Resources/bootstrap.json" \
   --attempt "$W/attempt" --choices "$W/attempt/choices.json" --bundle "$APP/Contents/Resources/bundle" \
   --os-version "$(sw_vers -productVersion)" --prepare-only > "$W/engine.log" 2>&1) && echo "engine OK in $(( $(date +%s) - start ))s, $(grep -c '^@@ ' "$W/engine.log") progress events" || { tail -5 "$W/engine.log"; exit 1; }
step "unattended install with People (from a fresh shell: resume record, no receipt variable)"
start=$(date +%s)
(cd ~/FamilyFoto && printf '%s\n' "$PASSWORD" | env -i HOME="$HOME" PATH=/usr/bin:/bin:/usr/sbin:/sbin bash installer/macos/install.sh install --with-people --research-only --password-stdin > "$W/install.log" 2>&1) \
  && echo "install OK in $(( $(date +%s) - start ))s: $(grep -E 'Installation complete' "$W/install.log" | head -1 | cut -c1-60)" || { tail -8 "$W/install.log"; exit 1; }
grep -o '"provider": "[a-z]*"[^}]*"state": "[a-z-]*"' "$W/install.log" | head -1
step "start, stop, start"
cd ~/FamilyFoto
health() { for _ in $(seq 1 40); do curl -sk --max-time 2 https://127.0.0.1:4341/api/macos-health >/dev/null && return 0; sleep 1; done; return 1; }
.venv/bin/python3 installer/macos/manage.py start 2>&1 | head -1; health && echo "health OK; sign-in page HTTP $(curl -sk -o /dev/null -w '%{http_code}' https://127.0.0.1:4341/login)"
start=$(date +%s); timeout 120 .venv/bin/python3 installer/macos/manage.py stop 2>&1 | tail -1; echo "stop took $(( $(date +%s) - start ))s"
.venv/bin/python3 installer/macos/manage.py start 2>&1 | head -1; health && echo "health OK after restart"
step "installed checks"; bash "$KIT/qualify.sh" installed ~/FamilyFoto | tail -8
step "offline: block the internet, install again into a second folder"
# Loopback stays open: FamilyFoto talks to its own database and web server on 127.0.0.1, and a
# blocked close there leaves PostgreSQL holding a dead connection that later blocks stop and backup.
printf 'set skip on lo0\nblock drop out quick inet proto {tcp udp} from any to ! 192.168.64.0/24\nblock drop out quick inet6 all\n' > /tmp/ff-offline.pf
sudo -n pfctl -q -f /tmp/ff-offline.pf 2>/dev/null; sudo -n pfctl -q -e 2>/dev/null
bash "$KIT/qualify.sh" offline | tail -1
O=$(mktemp -d "$HOME/ff-offline.XXXX"); mkdir -p "$O/attempt"; printf '{"root": "%s", "people": true, "agreement_digest": "%s"}' "$HOME/FamilyFoto-offline" "$(shasum -a 256 "$AG" | cut -c1-64)" > "$O/attempt/choices.json"
(cd "$W/engine" && "$W/python/bin/python3.12" -E -s -B -m installer.macos.online_setup --descriptor "$APP/Contents/Resources/bootstrap.json" \
   --attempt "$O/attempt" --choices "$O/attempt/choices.json" --bundle "$APP/Contents/Resources/bundle" \
   --os-version "$(sw_vers -productVersion)" --prepare-only > "$O/engine.log" 2>&1) && echo "offline engine OK" || tail -3 "$O/engine.log"
V="$O/v"; ~/FamilyFoto-offline/bundled/python/bin/python3.12 -m venv "$V"
(cd ~/FamilyFoto-offline && FAMILYFOTO_PAYLOAD_RECEIPT="$O/attempt/prepared.json" "$V/bin/python" -m installer.common.install_dependencies --role core >/dev/null 2>&1) \
  && "$V/bin/python" -c 'import fastapi, psycopg, PIL, pillow_heif; print("offline core install OK")'
sudo -n pfctl -q -d 2>/dev/null; sudo -n pfctl -q -F all 2>/dev/null
echo "network restored: HTTP $(curl -s -o /dev/null -w '%{http_code}' --max-time 8 https://github.com)"
