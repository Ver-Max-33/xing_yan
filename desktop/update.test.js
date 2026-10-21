// node --test update.test.js
const assert = require("node:assert")
const { spawnSync } = require("node:child_process")
const fs = require("node:fs")
const os = require("node:os")
const path = require("node:path")
const test = require("node:test")
const { MAC_SWAP, bundleToReplace, installerFor } = require("./update")

test("only a newer version's installer for this system, from the update folder, is run", () => {
  const dir = path.join(os.tmpdir(), "xingyan-updates")
  assert.equal(installerFor("0.2.1", dir, "win32", "0.2.0"), path.join(dir, "xingyan-0.2.1-win-x64.exe"))
  assert.equal(installerFor("1.0.0", dir, "darwin", "0.9.12"), path.join(dir, "xingyan-1.0.0-mac-arm64.dmg"))
  assert.equal(installerFor("0.10.0", dir, "win32", "0.9.0"), path.join(dir, "xingyan-0.10.0-win-x64.exe"))   // not text order
  for (const v of ["0.2.0", "0.1.9", "0.2.1-beta", "../../x", "", null, 1]) assert.equal(installerFor(v, dir, "win32", "0.2.0"), null)
  assert.equal(installerFor("0.2.1", dir, "linux", "0.2.0"), null)
})

test("macOS: the app is replaced only where it was installed and can be written", () => {
  const yes = () => true
  assert.equal(bundleToReplace("/Applications/形研.app/Contents/MacOS/形研", yes), "/Applications/形研.app")
  assert.equal(bundleToReplace("/Volumes/形研 0.2.0/形研.app/Contents/MacOS/形研", yes), null)   // run from the disk image
  assert.equal(bundleToReplace("/private/var/folders/x/T/AppTranslocation/A1/d/形研.app/Contents/MacOS/形研", yes), null)
  assert.equal(bundleToReplace("/Users/me/repo/desktop/node_modules/electron/dist/Electron", yes), null)
  assert.equal(bundleToReplace("/Applications/形研.app/Contents/MacOS/形研", (p) => p !== "/Applications"), null)
})

/** A disk image like the release's: the new app (a stand-in bundle saying its version) beside an Applications link. */
function diskImage(dir, version) {
  const src = path.join(dir, "src")
  fs.mkdirSync(path.join(src, "形研.app", "Contents"), { recursive: true })
  fs.writeFileSync(path.join(src, "形研.app", "Contents", "version"), version)
  fs.symlinkSync("/Applications", path.join(src, "Applications"))
  const dmg = path.join(dir, `xingyan-${version}-mac-arm64.dmg`)
  const r = spawnSync("hdiutil", ["create", "-quiet", "-fs", "HFS+", "-srcfolder", src, "-volname", "形研", "-format", "UDZO", "-ov", dmg])
  assert.equal(r.status, 0, String(r.stderr))
  return dmg
}

function installed(dir, version) {
  const app = path.join(dir, "Applications", "形研.app")
  fs.mkdirSync(path.join(app, "Contents"), { recursive: true })
  fs.writeFileSync(path.join(app, "Contents", "version"), version)
  return app
}

/** The swap as main.js starts it, the opener `echo` saying what would be opened; `pid` an app that ends a moment later
 * (started through a shell that leaves at once, so that, like the app, nothing here has to reap it). */
function swap(dmg, app) {
  const pid = spawnSync("/bin/sh", ["-c", "sleep 0.5 >/dev/null 2>&1 & echo $!"], { encoding: "utf8" }).stdout.trim()
  const r = spawnSync("/bin/sh", ["-c", MAC_SWAP, "sh", pid, dmg, app, "echo"], { encoding: "utf8", timeout: 60_000 })
  return { opened: r.stdout.trim(), left: fs.readdirSync(path.dirname(app)) }
}

test("macOS: once the app has quit the new one takes its place and opens; otherwise the old one stays and the image opens",
  { skip: process.platform !== "darwin" }, () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "swap-"))
    const dmg = diskImage(dir, "9.0.0")
    const app = installed(dir, "0.2.1")
    const began = Date.now()
    assert.deepEqual(swap(dmg, app), { opened: app, left: ["形研.app"] })
    assert.ok(Date.now() - began >= 500)                                            // it waited for the app to end
    assert.equal(fs.readFileSync(path.join(app, "Contents", "version"), "utf8"), "9.0.0")
    assert.ok(!/xingyan-update\./.test(spawnSync("hdiutil", ["info"], { encoding: "utf8" }).stdout))   // the image let go

    assert.deepEqual(swap(path.join(dir, "missing.dmg"), app), { opened: path.join(dir, "missing.dmg"), left: ["形研.app"] })
    fs.chmodSync(path.dirname(app), 0o555)                                          // a folder this user cannot write
    try {
      assert.deepEqual(swap(dmg, app), { opened: dmg, left: ["形研.app"] })
    } finally {
      fs.chmodSync(path.dirname(app), 0o755)
    }
    assert.equal(fs.readFileSync(path.join(app, "Contents", "version"), "utf8"), "9.0.0")
    fs.rmSync(dir, { recursive: true, force: true })
  })
