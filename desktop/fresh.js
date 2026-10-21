// In development the window shows frontend/dist, and nothing rebuilt it by itself: a change in the repository did not
// show until someone remembered to build. On 2026-10-07 a build from before the backend's versioned boxes drew every old
// version of a box at once (the chart full of stacked purple boxes). On start, sources newer than the build mean a build
// first. Development only (main.js requires it lazily): the installed app carries its own build.
const fs = require("node:fs")
const path = require("node:path")

const SOURCES = ["src", "index.html", "package.json", "vite.config.ts", "tsconfig.json", "tsconfig.app.json"]

/** The newest modification time under `p` (0 when it does not exist). */
function newest(p) {
  let st
  try { st = fs.statSync(p) } catch { return 0 }
  if (!st.isDirectory()) return st.mtimeMs
  return fs.readdirSync(p).reduce((m, f) => Math.max(m, newest(path.join(p, f))), st.mtimeMs)
}

/** True when `fe`/dist is missing or older than any of its sources. */
function stale(fe) {
  const built = newest(path.join(fe, "dist", "index.html"))
  return built === 0 || SOURCES.some((s) => newest(path.join(fe, s)) > built)
}

module.exports = { stale }
