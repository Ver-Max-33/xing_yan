# Builds the Windows installer (spec §3.3, §13 P4): the frontend, the PyInstaller backend (checked against the
# source), then the app with electron-builder (NSIS). Run it on Windows: PyInstaller does not cross-compile.
# Unsigned (P7): Windows SmartScreen asks once before the first run. Output: desktop\dist\*.exe
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
function Step($dir, $cmds) {
  Push-Location $dir
  try { foreach ($c in $cmds) { Write-Host "> $c"; Invoke-Expression $c; if ($LASTEXITCODE) { throw "失败：$c" } } }
  finally { Pop-Location }
}
Step "frontend" @("npm ci", "npm run build")
Step "backend" @("uv sync --frozen --group build",
  "uv run --group build pyinstaller packaging/wb.spec --noconfirm --distpath dist --workpath build",
  "uv run python packaging/check_build.py")
Step "desktop" @("npm ci", "npm run electron:install", "npm run dist:win")
Get-ChildItem desktop\dist\*.exe
