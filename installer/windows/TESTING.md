# Test a Fresh Windows Installation

Use a clean Windows 11 x64 machine or persistent VM, internet access, at least
10 GB free on local NTFS for Core (additional space for People/media), and an
administrator account. A persistent VM is needed to test imports and reboot.
Windows Sandbox cannot validate all disk-identity checks.

Do not run Install in the live FamilyFoto directory. A different folder on the
same server is not sufficient: setup uses fixed service/task names and default
ports 5432/4340, and refuses an existing installation even if its app is stopped.
Do not remove the live task/service or bypass those guards to make testing work.

## Get the Code

Download ZIP from the repository's `main` branch and
extract the complete application so `C:\FamilyFoto\installer` exists. Do not
download `main` or copy only the installer folder. Git users can instead run:

```powershell
git clone --branch main --single-branch https://github.com/joeandblack/FamilyFotoPub.git C:\FamilyFoto
```

No Git, Node, Codex or Docker is required after downloading the source. Python,
PostgreSQL and FFmpeg are provisioned by Core setup. Do not copy live credentials,
database, media, model profiles or matching policies into the new installation.

## Install and Verify

Open **Windows PowerShell as administrator**, using the account that will run
FamilyFoto, then:

```powershell
cd C:\FamilyFoto
powershell.exe -NoProfile -ExecutionPolicy Bypass -File installer/windows/install.ps1 -Mode Plan -WithPeople
```

Plan does not install anything. `Bypass` here applies only to that PowerShell
process; it does not change the machine's saved execution policy. Review the code
and [model terms](../../face_detect/README.md) before running downloaded scripts.
Only use `-ResearchOnly` if the documented non-commercial research terms apply
and you accept them. Then install and verify before importing any media:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File installer/windows/install.ps1 -Mode Install -WithPeople -ResearchOnly
powershell.exe -NoProfile -ExecutionPolicy Bypass -File installer/windows/install.ps1 -Mode Verify -WithPeople
```

Enter a new FamilyFoto administrator password when prompted; database secrets are
generated automatically. Source/destination media folders are not requested yet.
If an operation fails, retain its error and logs rather than deleting setup state.
Core Resume is for interrupted Core setup only. If Core completed but People
failed, inspect that separate failure; do not reinstall Core over it.

## Exercise the App

1. Open **FamilyFoto Server** on the desktop and start it. Visit
   `https://127.0.0.1:4340/`; sign in as `family` using the password set above.
2. In Imports choose a small test source and a separate empty destination. Use
   disposable copies, not the live library/destination. Import a few photos.
3. In Settings explicitly start Face detection service. Keep **Start face service
   with app** disabled. Start Discover people for the test import yourself.
4. Check thumbnails, group review, naming/moving faces, pause/resume and album
   operations. A new uncalibrated profile is review-only: it does not inherit the
   live catalog's automatic matching threshold or named people.
5. Stop FamilyFoto gracefully, reboot the test machine, then open the desktop
   launcher again. Confirm imported media and review decisions persist. No face
   scan should automatically resume; start the service explicitly when needed.

`Verify -WithPeople` checks an empty fresh installation and is not intended for
use after imports. Full clean-VM/reboot acceptance remains pending; consult
[recorded results](TEST_RESULTS.md) for the narrower tests already completed.

For maintainer-only same-host isolated native testing, see
`test-native-people.py --help`. That harness requires existing PostgreSQL binaries
and a sanitized test package; it is not the full end-user installer.
