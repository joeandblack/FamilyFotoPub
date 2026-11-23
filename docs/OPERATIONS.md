# Operations

Running a FamilyFoto server: network access, HTTPS, accounts, the database, disks, the
desktop launcher and status. Installation itself is in [`../installer/`](../installer/README.md).

Contents: [Network access](#network-access) · [HTTPS](#https) · [Accounts and sessions](#accounts-and-sessions) ·
[Database](#database) · [Removable disks](#removable-disks) · [Desktop launcher](#desktop-launcher) ·
[Face detection service](#face-detection-service) · [Status indicator](#status-indicator) ·
[Performance notes](#performance-notes) · [Read-only preview](#read-only-preview)

## Network access

FamilyFoto is served to browsers only; PostgreSQL and the media disks are never shared. To use
it from other computers or phones on the same trusted home network (not a guest Wi-Fi):

1. In private `config/local.json` set the existing top-level fields `"host": "0.0.0.0"` and
   `"port": 4340`. Keep `database.host` as `127.0.0.1`.
2. Find the server's IPv4 address (`ipconfig`), for example `192.0.2.10`, and mark only your
   trusted home connection as **Private** in Windows. `0.0.0.0` is the listening address, not
   the one to type in a browser. A DHCP reservation in the router keeps the address stable.
3. In administrator PowerShell, allow the port for the private profile and local subnet only:

   ```powershell
   New-NetFirewallRule -Name 'FamilyFoto-LAN-4340' `
     -DisplayName 'FamilyFoto home network (4340)' `
     -Direction Inbound -Action Allow -Protocol TCP -LocalPort 4340 `
     -LocalAddress '192.0.2.10' -RemoteAddress LocalSubnet -Profile Private
   ```

   Create it once and update it if the address changes. Never disable the firewall, allow the
   Public profile, forward 4340 to the Internet or expose PostgreSQL's 5432.
4. Restart only the web server (`app.service.server`), not workers or PostgreSQL, and open
   `https://<server address>:4340/` on the other device.

If the page won't open, check the web server is running, Windows uses the Private profile, the
firewall rule matches the current address, and neither device is on guest Wi-Fi, a VPN or
another subnet.

## HTTPS

FamilyFoto never serves plain HTTP. Every launcher (`app.service.server`, `app.service.desktop`,
`app.service.macos`, the read-only preview) refuses to start unless `tls.certfile` and
`tls.keyfile` are set and loadable.

- **Certificates.** `installer/configure.py` creates a private certificate authority and a
  server certificate in `config/tls/` (ignored by Git) and records them in `config/local.json`.
  The computer's name and LAN address are included automatically; `--tls-name` adds more DNS
  names or addresses, and `--tls-cert/--tls-key` use your own certificate. Relative paths resolve
  from the project root. An incomplete or invalid `tls` setting fails at startup instead of
  falling back to HTTP.
- **Renewal.** The server certificate lasts 800 days. Each start re-issues it from the same CA
  within 30 days of expiry or when the computer's names change, so clients keep their trust.
  `python -m app.certificates [--name host-or-ip]` does the same by hand.
- **Clients.** Install `config/tls/ca.pem` once on each browser computer as a trusted root, after
  checking its SHA-256 fingerprint out of band. On a Mac: Keychain Access > File > Import Items
  into the login keychain, then set **Secure Sockets Layer (SSL)** to **Always Trust**. On
  Windows: Install Certificate > Current User > Trusted Root Certification Authorities. Some
  browsers keep their own store. Never bypass a certificate warning, and never copy
  `ca-key.pem` off the server. Trusting the CA trusts every certificate it can issue.
- **Behaviour.** TLS 1.2 minimum. Login cookies are Secure, HttpOnly and SameSite=Strict, and the
  same-origin check uses the real request URL. Forwarded headers are ignored and can't choose
  the scheme. There's no HSTS or HTTP redirect, and no IP allowlist.
- The loopback link between the web app and the People helper (127.0.0.1) stays plain HTTP.
- `tests/test_https.py` covers configuration, protocol minimum, cookie attributes, origin
  handling and spoofed forwarded headers in-process.

**Development only:** `FAMILYFOTO_DEV_HTTP=1 python -m app.service.server` serves plain HTTP on
`127.0.0.1` or `localhost` only, with a warning. It is an environment variable, never a saved
setting; it never weakens a configured certificate; and `installer/verify-installation.py` and
the installers still require HTTPS, so a release can't depend on it.

## Accounts and sessions

The `browser_user` account is the main administrator; its credential is `browser_password_hash`
in private `config/local.json`. Other accounts live under `browser_accounts` with a role and
`password_hash`. All passwords use PBKDF2-HMAC-SHA256 with 600,000 iterations and random 128-bit
salts; plaintext `browser_password` or `password` fields are refused. A random 256-bit
`auth_secret` binds session credentials and signs download tickets.

| Role | Can |
| --- | --- |
| Admin | Everything, including permanent deletion and managing users |
| Regular (shown as **Normal**) | Everything except permanent deletion and administration |
| Guest | Read-only browsing and downloads; can sign out their own sessions |

Checks run on the server, including from localhost; hidden UI controls are not the boundary.

- **Managing users.** In Settings, administrators can create a user (a generated password is
  offered; role Admin, Normal or Guest) and reset a user's password. Resetting signs that user
  out everywhere; resetting your own password signs you out at once, so the page asks you to
  confirm the new password is saved first. Settings writes hold a cross-process lock
  (`config/local.json.lock`), which backups never capture.
- **Sessions.** Cookies hold random 256-bit opaque tokens; PostgreSQL stores only their SHA-256
  digests, account names, credential signatures and timestamps (`auth_sessions`, schema 025, in
  the primary catalog). Sessions survive server restarts. A session is guaranteed for 12 hours
  after sign-in, then lasts while used, expiring after one hour without genuine mouse, touch,
  wheel or keyboard activity (reported at most every 30 seconds). Polling and open tabs don't
  renew it. Every request checks the current password hash, role, account and `session_epoch`;
  changing any of them, or `auth_secret`, invalidates affected sessions.
- **Sign-out.** Sidebar **Sign out** deletes the current session. **Settings > Account security >
  Sign out all my sessions** revokes the account's sessions; administrators can also sign out a
  chosen account without changing its password. Revocation applies to later requests; running
  media work is unaffected.
- **Login limits.** A shared PostgreSQL table (`auth_login_limits`) admits at most 5 attempts per
  username, 20 per client IP and 60 overall in each 60-second window, reserved atomically before
  hashing. Unknown users get the same error and hashing work as wrong passwords. Keys are HMACs,
  and the socket peer (never a forwarded header) gives the IP. Blocked requests get HTTP 429 with
  `Retry-After`. Database failure fails closed. These limits suit a small private network, not a
  public service.
- Fresh installs: `installer/configure.py` asks (without echo) for an administrator password of
  at least 12 characters and stores only its hash. Older installs with plaintext credentials:
  stop both HTTP services, run `python scripts/upgrade_auth.py` (it hashes them in place,
  creates `auth_secret` and keeps the file's permissions), start the server and sign in again.
- Protect configuration and database backups: hashes still need protection against offline
  guessing.

References: [OWASP Authentication](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html)
and [Session Management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html).

## Database

PostgreSQL 17. On Windows it runs as the native service `FamilyFotoPostgres`, with binaries in
`runtime/postgres` and the cluster in `data/postgres` on the local application disk (never on a
removable media disk). The Mac installer bundles its own PostgreSQL. The application uses the
non-superuser login and database `familyfoto`; generated credentials are in `config/local.json`,
readable only by the operator, SYSTEM and administrators. PostgreSQL listens on loopback only.

**Windows fresh install** (the installer normally does this):
1. Download PostgreSQL 17.11-5 Windows x64 from the official EDB distribution
   (https://get.enterprisedb.com/postgresql/postgresql-17.11-5-windows-x64.exe) to
   `temp/postgresql-17.11-5-windows-x64.exe`. SHA-256:
   `612c7f5400a003aaffa0372cb273eed52278f93c680ab911eb9cb2229e329072`.
2. In administrator PowerShell run `installer/windows/install-postgres.ps1`. It verifies the
   installer and refuses to install over an existing service or cluster.
3. Run `python installer/init-database.py` (role, database, loopback-only listening), then
   `Restart-Service FamilyFotoPostgres`, and confirm `Get-NetTCPConnection -LocalPort 5432
   -State Listen` shows only `127.0.0.1`.
4. Run `python -m app.database.schema_upgrading` to apply schema upgrades
   (`python -m app.database` is a compatibility entry point).

The pinned installer is for reproducibility, not perpetual security support; update its URL and
hash deliberately.

**Schema upgrades.** SQL lives in `databases/NNN_name.sql` with consecutive unique versions,
recorded in `schema_versions`. `app/database/schema_upgrading.py` owns discovery, version checks,
admission guards and transactional execution. When nothing is pending, startup runs no SQL and
takes no locks. When upgrades are pending, the runner serialises competing upgrades, rechecks
versions under the migration lock, and takes the worker guards (five-second bounded wait) before
any DDL. Failure rolls back. A schema newer than the application is refused. Never edit an
applied migration; add a new one. Upgrading an existing library needs a freshly verified backup
(see [Backup](BACKUP.md#schema-upgrades)). `app/database/locks.py` names the advisory lock
identities; they are a cross-process protocol, so never renumber them.

**Connection pool.** `app/database/pool.py` keeps a process-local Psycopg pool per connection
configuration (credentials and `search_path` included), so the Library and People schemas never
share sessions: up to eight connections each, idle ones retired after 60 seconds, at most four
configurations per process, five-second checkout wait with at most 64 queued callers. Contexts
commit on success and roll back on failure. `DISCARD ALL` runs before a connection is reused, so
leaving a context releases its advisory locks; prepared statements are disabled. HEIF repair
owners use `database.dedicated_connection()` because their backend PID is part of the
cancellation protocol.

**Maintenance.** Autovacuum stays on; keep free disk space for WAL and growth. Check the cluster
`log/` and application `logs/` after faults. Use a password prompt or protected password file
with psql/pg_dump, never credentials in commands or history. Minor updates need a planned stop;
major versions need `pg_upgrade` or dump and restore.

**Reinstalling.** Reinstalling application code doesn't require replacing a working database.
Keep `config/local.json`, `data/postgres`, thumbnails and destination media. Never run `initdb`
on an existing cluster or delete data to fix a connection problem. To move a library to another
computer, use [backup and restore](BACKUP.md); a live cluster can't be copied like a file.

## Removable disks

A drive letter can be reused by another device, so FamilyFoto never trusts the letter or label
alone. Each disk role is enrolled in machine configuration with its physical hardware serial,
filesystem volume serial, Windows volume GUID, drive letter and root. Before an import all must
match and both disks must be online. A `.familyfoto-volume.json` marker is created on the
destination only; the source is never written. During copying, volume serial checks and the
marker guard against letter reuse, and a background monitor checks physical identity every five
seconds (a run fails if 60 seconds pass without a healthy check).

- **Keep a familiar letter** with Disk Management > Change Drive Letter and Paths. FamilyFoto
  never changes letters, formats disks, repairs filesystems or clears device errors.
- **Same disk, new letter:** pause the importer, identify the disk by its serials, then restore
  the old letter or update only `drive` and `root` in `config/local.json`. Never replace identity
  fields to bypass an error; a different or reformatted disk needs re-enrolment and review.
- **After a storage error,** verify identities and check cataloged destination files for
  existence and size. Missing copies can be requeued from their import items: the importer
  restores from the source, verifies SHA-256 and keeps the media ID. A destination with the wrong
  checksum is never overwritten. An existence/size audit is not a checksum audit. Investigate
  repeated I/O errors (power, cables, enclosure, device health) before continuing.

## Desktop launcher

On Windows, double-click **FamilyFoto Server** on the desktop. **Start Server** runs FamilyFoto
through an on-demand scheduled task as the signed-in user (no startup trigger, timeout or
automatic restart; PostgreSQL must already be running). **Open FamilyFoto** opens the configured
URL. Closing the window doesn't stop the server.

Pause imports, video work and conversions before **Stop Server**. Stop requests a graceful
shutdown: HTTP connections get up to 30 seconds to drain, and it never kills a process tree,
decoder, independent worker or the database. Wait for the task to show Ready before restarting.
Errors go to `logs/desktop-server.log`. Install or repair the shortcut and task with
`installer/windows/install-desktop-launcher.ps1`; no credentials or keys are embedded.

## Face detection service

Settings has an administrator-only **Face detection service** section. **Start** starts the
native People helper with its installed provider profile; **Stop** requests pauses at photo
boundaries first. **Start face detection with FamilyFoto** (off by default, saved in
`data/face-service-settings.json`) starts it asynchronously with the web app, so Library startup
isn't blocked. Starting the service never starts or resumes a scan; use Start/Resume in Imports.
The dot is steady green when ready, pulsing during start/stop, gray when stopped or unknown, and
red when unavailable. A service failure doesn't stop Core. These controls don't install People;
see [People](PEOPLE.md#setup).

## Status indicator

The bottom navigation dot shows the latest reported processing state, not whether a process
exists. Idle is **Ready**, never "Not running", and text and hover descriptions accompany colour.

| Situation | Dot | Label |
| --- | --- | --- |
| Status not known yet | Steady gray | Initializing... |
| Connected, nothing active | Steady green | Ready |
| Import, video processing, conversion or duplicate scan running | Pulsing green | Importing / Process videos / Convert videos / Scan dups, with active time |
| Discover people running, queued or yielding | Pulsing green | Discover people (hover shows state and counts) |
| Required service unavailable (`waiting-service`) | Steady amber | Waiting for service |
| Job blocked, failed or paused with an error | Steady red | Stopped with error (hover names it) |
| Active non-People job with a missing or stale (over 60 s) monitor | Steady red | Check status |
| Status request fails | Steady gray | Connection lost (server jobs may still run) |
| Paused, cancelled, completed or warnings only | Steady green | Ready (details stay on Imports) |

An active operation beats an older error or service wait; among active ones the order is
duplicate scan, conversion, video processing, import, Discover people. Without one, errors beat
service waits. The main status polls every five seconds; People status is fetched separately and
keeps its last result through a transient failure. A failed main poll clears the cached snapshot
so a late People reply can't restore a stale Ready. Reduced-motion preferences stop the pulse.
Implementation: `web/operation-status.mjs`, applied by `web/app.js`, styled in `web/styles.css`.

## Performance notes

- Validated settings are cached per process and reread only when the file's identity, size or
  timestamps change; an invalid changed file fails closed. Authentication verifies the cookie
  once per request; there's no cross-request user cache.
- The importer reuses its connection for progress and publishes progress at most every five
  seconds (pause, failure and completion immediately). Each copied file still has its own
  transaction, recorded only after the destination is flushed, its SHA-256 matches, the source
  size and mtime are unchanged and disk checks pass.
- `data/performance.json` holds cumulative timings for the current importer run (`source_hash`,
  `copy_read_write`, `copy_source_read`, `destination_write`, `destination_flush`,
  `destination_verify`, `existing_copy_verify`, `thumbnail_metadata`, the `database_*` timings,
  `progress_reporting`, `completion_logging`, `throttle_wait`). They are nested, so don't add
  them up, and they include OS cache effects.
- Import traffic is capped at 30 MiB/s in total. A new copy costs about four times its size (hash
  source, copy, write, verify) plus thumbnail reads.

## Read-only preview

`scripts/launch_readonly_preview.py` starts `app.service.readonly_preview`, an explicit test
entry point that reads an existing library without migrating it or running workers: a
SELECT-only database role verified at startup, a lifespan that skips upgrades and workers,
middleware that rejects modifying requests (People through the GET-only allowlist in
`face_detect/api/readonly_people.py`), a Windows Low Integrity token checked with a protected
canary, and writes limited to a private LocalLow scratch folder. Sessions are in memory only.
It is not the normal server and does not start automatically.
