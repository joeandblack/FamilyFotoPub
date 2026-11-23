Restore this FamilyFoto backup on a Mac

This backup can be restored on an Apple Silicon Mac whether it was made on a Mac or on Windows.
Restoring installs FamilyFoto and puts this library into it in one go.

Before you begin
- Know the FamilyFoto administrator password from when this backup was made. A later password
  change does not change an existing backup.
- Finish the backup and stop FamilyFoto on the old computer before switching to the restored
  library.
- Do not run programs merely because they are on this disk.

Which way to restore
- If this folder contains RELEASE.txt, this backup was made by a released FamilyFoto
  installation: use FamilyFoto Setup, as RELEASE.txt and "With FamilyFoto Setup" below say.
- Otherwise this backup was made by an installation set up from source code: use the Terminal
  launcher in this folder, as "With the Terminal launcher" below says.

With FamilyFoto Setup (macOS 27 or newer; nothing else to install first)
1. Download FamilyFotoSetup.dmg from the link in RELEASE.txt and check its SHA-256 as it says.
   Do not open it if the checksum differs.
2. Open the disk image and drag FamilyFoto Setup to Applications. The app is not notarized by
   Apple: open it once, then approve it in System Settings, Privacy & Security, Open Anyway.
3. In FamilyFoto Setup, accept the terms, choose Restore from a backup and choose this backup
   folder: the folder that holds catalog and this restore-on-mac folder.
4. Choose an empty installation folder (not inside the backup), and whether to include People.
   Without People, saved people stay viewable.
5. Click Install and Restore. Setup downloads and checks FamilyFoto, then opens the setup
   window with this backup filled in. Continue with "In the setup window" below.

With the Terminal launcher (needs Command Line Tools, Homebrew and Homebrew Python 3.12)
The launcher downloads exactly the FamilyFoto version that made this backup (the commit
written inside it) from the public FamilyFotoPub repository on GitHub, over HTTPS. This Mac
needs internet access; no GitHub account is needed.
1. Open Terminal. Create and enter an empty installation folder first, for example:
     mkdir -p ~/FamilyFoto-Restore
     cd ~/FamilyFoto-Restore
   If that folder already contains files, choose a different empty folder. Then type bash
   followed by a space.
2. Drag restore-familyfoto-mac.command from this folder into Terminal and press Return.
3. Enter a fresh installation folder on the Mac. Press Return to use the current Terminal
   directory (which must be empty).
4. The launcher downloads FamilyFoto, prepares temporary password-checking dependencies, then
   compiles and opens the setup window with this backup preselected. The first window launch can
   take a minute. If the launcher was moved out of the backup, choose the backup with Browse.

In the setup window
1. Enter the administrator password from when this backup was made, then click Check password.
   Errors appear in red. The password is not saved. Older recovery.key backups need their
   original release; this version accepts password backups only.
2. Choose how to use the photos and videos.
   Copy keeps the backup and needs room for another copy of the photos and videos. Setup
   defaults to FFLibrary beside the backup folder; use Browse to choose another folder.
   In place uses the backup disk as the library disk: setup moves the media into FFLibrary
   beside the backup folder with one folder rename. This uses up the backup. To use another
   location, choose a parent folder on the same disk, outside the backup folder.
3. With People, accept its model terms. (The Terminal launcher asks here whether to install it.)
4. Check the answers. The old-computer-stopped checkbox is selected by default. The
   unprotected-disk acceptance checkbox, when shown, is also selected by default. Clear either if
   it does not reflect your decision.
5. Click Start and keep the window open and the disks connected until setup finishes.
6. Click Open FamilyFoto and sign in with the old library's accounts. If the browser warns about
   the certificate, use Trust this computer's certificate and approve the macOS prompt.

If setup stops
Keep the installation folder. In Terminal, change to that folder and run:
  bash installer/macos/install.sh restore
This carries on using the recorded choices. Enter the password again if requested. After
thumbnails finish, setup can continue without the backup disk or password. If it says the
downloaded installer files are gone, open FamilyFoto Setup again and choose the same
installation folder. Interrupted Core or People installation may need inspection.

After restoring
Check your photos and albums, then make and verify a new backup.
