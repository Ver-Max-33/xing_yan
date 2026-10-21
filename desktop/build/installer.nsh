; The installer writes the app's files straight into the install folder, once, with NSIS's own File /r: by default
; electron-builder unpacks a 7z of ~2,000 files into a temp folder, copies them all over again, and then copies the
; installer itself into %LOCALAPPDATA% for electron-updater (which this app does not use) — slow (each file written
; twice, each write scanned by antivirus) and the progress bar fills, starts over, then stalls. Its installer.nsh
; takes APP_BUILD_DIR for this: the folder electron-builder packs, win-unpacked under directories.output (dist).
!define APP_BUILD_DIR "${PROJECT_DIR}\dist\win-unpacked"
