Restore this FamilyFoto backup on a Mac

Before you begin
- Use an Apple Silicon Mac with Command Line Tools, Homebrew and Homebrew Python 3.12 installed.
- The launcher downloads the release/0.1 branch of the public FamilyFotoPub repository from GitHub over HTTPS. This Mac needs internet access; no GitHub account is needed.
- Know the FamilyFoto administrator password from when this backup was made. A later password change does not change an existing backup.
- Finish the backup and stop FamilyFoto on the old computer before switching to the restored library.

If this folder contains RELEASE.txt, this backup was made by a released FamilyFoto installer:
follow RELEASE.txt to install that release with its own setup app instead of the steps below.

Launch setup
1. Open Terminal. Create and enter an empty installation folder first, for example:
     mkdir -p ~/FamilyFoto-Restore
     cd ~/FamilyFoto-Restore
   If that folder already contains files, choose a different empty folder. Then type bash followed by a space.
2. Drag restore-familyfoto-mac.command from this folder into Terminal and press Return.
3. Enter a fresh installation folder on the Mac. Press Return to use the current Terminal directory (which must be empty).
4. The launcher downloads FamilyFoto, prepares temporary password-checking dependencies, then compiles and opens the setup window. The first window launch can take a minute.

In the setup window
1. The launcher preselects the backup folder containing this restore-on-mac folder, catalog, library and media-copy. Check it, or use Browse to choose another backup. If the launcher was moved out of the backup, choose the backup manually.
2. Enter the administrator password from when this backup was made, then click Check password. Errors appear in red. The password is not saved. Older recovery.key backups need their original release; this version accepts password backups only.
3. Choose how to use the media.
   Copy keeps the backup and needs room for another copy of the photos and videos. Setup defaults to FFLibrary beside the backup folder; use Browse to choose another folder.
   In place uses the backup disk as the library disk. Setup defaults to FFLibrary beside the backup folder. To use a different location, click Browse and choose a parent folder on that same disk, outside the backup folder. Setup creates FFLibrary inside the selected parent and moves the media with one folder rename. This consumes the backup.
4. Choose whether to install People and accept its model terms if you enable it. Saved people remain viewable when People is skipped.
5. Check the selected answers. The old-computer-stopped checkbox is selected by default. The unprotected-disk acceptance checkbox, when shown, is also selected by default. Clear either if it does not reflect your decision.
6. Click Start and keep the window open and disks connected until setup finishes.
7. Click Open FamilyFoto and sign in with the old library's accounts. If the browser warns about the certificate, use Trust this computer's certificate and approve the macOS prompt.

If setup stops
Keep the installation folder. In Terminal, change to that folder and run:
  bash installer/macos/install.sh restore
This carries on using the recorded choices. Enter the password again if requested. After thumbnails finish, setup can continue without the backup disk or password. Interrupted Core or People installation may need inspection.

After restoring
Check your photos and albums, then make and verify a new backup.
