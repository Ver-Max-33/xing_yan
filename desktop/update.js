// 下载并安装 (spec §3.3, 1.51): which downloaded installer the app may run, and how. The backend downloads it from the
// public repository's release and checks it against the release's SHA256SUMS.txt (backend wb/update.py); main.js runs it once every
// window has saved what it held and the backend has stopped. Kept apart from main.js so it can be tested without Electron.
const path = require("node:path")

// as the build workflow names them (electron-builder.yml artifactName; backend wb/update.py INSTALLER)
const INSTALLER = { win32: (v) => `xingyan-${v}-win-x64.exe`, darwin: (v) => `xingyan-${v}-mac-arm64.dmg` }

/** The installer to run for version `v`, if this app may run it: a plain version newer than the one running, the file
 * the backend downloaded into the update folder for this system. */
function installerFor(v, dir, platform, running) {
  const name = INSTALLER[platform]
  if (!name || typeof v !== "string" || !/^\d+\.\d+\.\d+$/.test(v)) return null
  const [a, b] = [v, running].map((s) => s.split(".").map(Number))
  const newer = a[0] - b[0] || a[1] - b[1] || a[2] - b[2]
  return newer > 0 ? path.join(dir, name(v)) : null
}

/** Windows: the installer's own update mode (electron-builder NSIS) — silent, into the folder the app is installed in
 * (the registry says which), the app opened again when it is done. */
const WINDOWS_ARGS = ["--updated", "/S", "--force-run"]

/** macOS: the app bundle this one runs from, if the new one can take its place — not when it runs from the disk image
 * or translocated (opened where it was downloaded, from a read-only copy), or this user cannot write its folder. */
function bundleToReplace(execPath, writable) {
  const bundle = path.resolve(execPath, "../../..")
  if (!bundle.endsWith(".app") || bundle.startsWith("/Volumes/") || bundle.includes("/AppTranslocation/")) return null
  return writable(path.dirname(bundle)) && writable(bundle) ? bundle : null
}

// macOS, run detached once the app has quit: the app in the disk image takes the old one's place and is opened; if any
// step fails the old app stays where it was and the disk image is opened instead, to drag the app over by hand.
// Arguments: the app's pid, the disk image, the bundle to replace, the command that opens the result.
const MAC_SWAP = `
pid=$1 dmg=$2 app=$3 open=$4
i=0; while kill -0 "$pid" 2>/dev/null && [ $i -lt 300 ]; do sleep 0.2; i=$((i+1)); done
mnt=$(mktemp -d "\${TMPDIR:-/tmp}/xingyan-update.XXXXXX") || { "$open" "$dmg"; exit 1; }
ok=
if hdiutil attach -nobrowse -noautoopen -readonly -mountpoint "$mnt" "$dmg" >/dev/null 2>&1; then
  src=; for a in "$mnt"/*.app; do [ -d "$a" ] && src=$a; done
  rm -rf "$app.new" "$app.old"
  if [ -n "$src" ] && ditto "$src" "$app.new" && mv "$app" "$app.old"; then
    if mv "$app.new" "$app"; then ok=1; rm -rf "$app.old"; else mv "$app.old" "$app"; fi
  fi
  rm -rf "$app.new"
  hdiutil detach "$mnt" -quiet || hdiutil detach "$mnt" -force -quiet
fi
rmdir "$mnt" 2>/dev/null
if [ -n "$ok" ]; then "$open" "$app"; else "$open" "$dmg"; fi
`

module.exports = { installerFor, WINDOWS_ARGS, bundleToReplace, MAC_SWAP }
