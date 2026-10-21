// node --test fresh.test.js
const assert = require("node:assert")
const fs = require("node:fs")
const os = require("node:os")
const path = require("node:path")
const test = require("node:test")
const { stale } = require("./fresh")

test("a build older than a source is stale, a newer one is not", () => {
  const fe = fs.mkdtempSync(path.join(os.tmpdir(), "fe-"))
  fs.mkdirSync(path.join(fe, "src", "chart"), { recursive: true })
  fs.writeFileSync(path.join(fe, "src", "chart", "paint.ts"), "x")
  assert.equal(stale(fe), true)                                   // never built
  fs.mkdirSync(path.join(fe, "dist"))
  fs.writeFileSync(path.join(fe, "dist", "index.html"), "x")
  const t = Date.now() / 1000
  fs.utimesSync(path.join(fe, "src", "chart", "paint.ts"), t - 60, t - 60)
  fs.utimesSync(path.join(fe, "src", "chart"), t - 60, t - 60)
  fs.utimesSync(path.join(fe, "src"), t - 60, t - 60)
  assert.equal(stale(fe), false)                                  // built after the last change
  fs.utimesSync(path.join(fe, "src", "chart", "paint.ts"), t + 60, t + 60)
  assert.equal(stale(fe), true)                                   // a file deep in src changed since
  fs.rmSync(fe, { recursive: true, force: true })
})
