// Pin every direct dependency to the newest stable version (same major) published before CUTOFF.
import { execFileSync } from "node:child_process"
import { readFileSync, writeFileSync } from "node:fs"

const CUTOFF = new Date(process.argv[2] ?? Date.now() - 7 * 864e5)
const pkg = JSON.parse(readFileSync("package.json", "utf8"))
const semver = (v) => v.split(/[.+-]/).slice(0, 3).map(Number)
const cmp = (a, b) => { const x = semver(a), y = semver(b); for (let i = 0; i < 3; i++) if (x[i] !== y[i]) return x[i] - y[i]; return 0 }
for (const section of ["dependencies", "devDependencies"]) {
  for (const [name, range] of Object.entries(pkg[section] ?? {})) {
    const major = Number(range.replace(/^[^\d]*/, "").split(".")[0])
    const times = JSON.parse(execFileSync("npm", ["view", name, "time", "--json"], { encoding: "utf8" }))
    const ok = Object.entries(times)
      .filter(([v, t]) => /^\d+\.\d+\.\d+$/.test(v) && new Date(t) < CUTOFF && semver(v)[0] === major)
      .map(([v]) => v).sort(cmp)
    const pick = ok.at(-1)
    if (!pick) throw new Error(`${name}: no stable ${major}.x release before ${CUTOFF.toISOString()}`)
    pkg[section][name] = pick
    console.log(`${section.slice(0, 3)} ${name.padEnd(32)} ${range.padEnd(10)} -> ${pick}  (${times[pick].slice(0, 10)})`)
  }
}
writeFileSync("package.json", JSON.stringify(pkg, null, 2) + "\n")
