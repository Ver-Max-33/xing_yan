#!/bin/bash
# Builds the macOS installer (spec §3.3, §13 P4): the frontend, the PyInstaller backend (checked against the source),
# then the app with electron-builder. Unsigned (P7). Output: desktop/dist/*.dmg
set -euo pipefail
cd "$(dirname "$0")/.."
(cd frontend && npm ci && npm run build)
(cd backend && uv sync --frozen --group build \
  && uv run --group build pyinstaller packaging/wb.spec --noconfirm --distpath dist --workpath build \
  && uv run python packaging/check_build.py)
(cd desktop && npm ci && npm run electron:install && npm run dist:mac)
ls -lh desktop/dist/*.dmg
