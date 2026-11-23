Restore this FamilyFoto backup on a Mac

This backup can be restored on a Mac whether it was made on a Mac or on Windows. Restoring
installs FamilyFoto and puts this library into it in one go, with FamilyFoto Setup.

Before you begin
- Use an Apple Silicon Mac with macOS 27 or newer. Nothing else needs to be installed first.
- Know the FamilyFoto administrator password from when this backup was made. A later password
  change does not change an existing backup.
- Finish the backup and stop FamilyFoto on the old computer before switching to the restored
  library.

Get FamilyFoto Setup
- If this folder contains RELEASE.txt, it names the exact release to use: follow it.
- Otherwise download FamilyFotoSetup.dmg from the official releases page,
    https://github.com/joeandblack/FamilyFotoPub/releases
  and check its SHA-256 against the checksum published on that release page:
    shasum -a 256 ~/Downloads/FamilyFotoSetup.dmg
  Do not open it if they differ.
- Open the disk image and drag FamilyFoto Setup to Applications. The app is not notarized by
  Apple: open it once, then approve it in System Settings, Privacy & Security, Open Anyway.
- Do not run programs merely because they are on this disk.

In FamilyFoto Setup
1. Read and accept the terms.
2. Choose Restore from a backup, then choose this backup folder: the folder that holds catalog
   and this restore-on-mac folder.
3. Choose an empty installation folder (not inside the backup), and whether to include People.
   Without People, saved people stay viewable.
4. Click Install and Restore. Setup downloads and checks FamilyFoto, then opens its setup window.

In the setup window
1. Enter the administrator password from when this backup was made, then click Check password.
   The password is not saved.
2. Choose how to use the photos and videos.
   Copy keeps the backup and needs room for another copy of the photos and videos. Setup
   defaults to FFLibrary beside the backup folder; use Browse to choose another folder.
   In place uses the backup disk as the library disk: setup moves the media into FFLibrary
   beside the backup folder with one folder rename, in minutes whatever the size. This uses up
   the backup. To use another folder, choose a parent folder on the same disk, outside the
   backup folder.
3. If People is included, accept its model terms.
4. Check the answers, including that the old computer's FamilyFoto is stopped, then click
   Start. Keep the window open and the disks connected until setup finishes.
5. Click Open FamilyFoto and sign in with the old library's accounts. If the browser warns
   about the certificate, use Trust this computer's certificate and approve the macOS prompt.

If setup stops
Keep the installation folder. In Terminal, change to that folder and run:
  bash installer/macos/install.sh restore
This carries on with the recorded choices. Enter the password again if asked. If it says the
downloaded installer files are gone, open FamilyFoto Setup again and choose the same
installation folder.

After restoring
Check your photos and albums, then make and verify a new backup.
