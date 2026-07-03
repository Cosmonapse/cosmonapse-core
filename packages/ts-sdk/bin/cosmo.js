#!/usr/bin/env node
/**
 * cosmo - npm launcher for the Cosmonapse developer CLI.
 *
 * The CLI has exactly one implementation: the Python package (`cosmonapse`
 * on PyPI). This launcher makes `npm install -g @cosmonapse/sdk` sufficient
 * to get a working `cosmo` WITHOUT a second CLI codebase:
 *
 *   1. If $COSMO_PYTHON is set, delegate to it (it must have `cosmo`).
 *   2. If a system Python already has the cosmonapse package, delegate to it.
 *   3. Otherwise BOOTSTRAP: create a private venv under ~/.cosmonapse/ and
 *      `pip install cosmonapse==<this package's version>` into it (one-time,
 *      re-run only when this package's version changes), then delegate.
 *
 * Every path ends in `python -m cosmo`, so pip and npm users always run the
 * same canonical CLI build. Requires a Python 3.11+ interpreter on PATH.
 */

import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

const WIN = process.platform === "win32";

// --- this package's version (pins the PyPI install for lockstep) -----------

function pkgVersion() {
  try {
    const p = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "package.json");
    return JSON.parse(readFileSync(p, "utf8")).version;
  } catch {
    return null;
  }
}

// --- interpreter discovery --------------------------------------------------

function interpreters() {
  const list = [];
  if (process.env.COSMO_PYTHON) list.push({ cmd: process.env.COSMO_PYTHON, args: [], pinned: true });
  if (WIN) list.push({ cmd: "py", args: ["-3"] });
  list.push({ cmd: "python3", args: [] });
  list.push({ cmd: "python", args: [] });
  return list;
}

function run(cmd, args, opts = {}) {
  return spawnSync(cmd, args, { windowsHide: true, ...opts });
}

function hasCosmo(py) {
  return run(py.cmd, [...py.args, "-c", "import cosmo"], { stdio: "ignore" }).status === 0;
}

function isPython311(py) {
  const r = run(py.cmd, [...py.args, "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"], { stdio: "ignore" });
  return r.status === 0;
}

// --- managed venv bootstrap ---------------------------------------------------

const VENV_DIR = path.join(
  process.env.COSMONAPSE_STATE_DIR ?? path.join(homedir(), ".cosmonapse"),
  "cli-venv",
);
const STAMP = path.join(VENV_DIR, ".cosmo-launcher-version");

function venvPython() {
  return WIN ? path.join(VENV_DIR, "Scripts", "python.exe") : path.join(VENV_DIR, "bin", "python");
}

function venvReady(version) {
  if (!existsSync(venvPython())) return false;
  try {
    return version === null || readFileSync(STAMP, "utf8").trim() === version;
  } catch {
    return false;
  }
}

function pyVersionOf(py) {
  const r = run(py.cmd, [...py.args, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"], { encoding: "utf8" });
  return r.status === 0 ? (r.stdout ?? "").trim() : null;
}

function bootstrap(version) {
  const base = interpreters().find((py) => !py.pinned && isPython311(py));
  if (!base) {
    for (const py of interpreters()) {
      const v = !py.pinned && pyVersionOf(py);
      if (v) {
        process.stderr.write(
          `cosmo: found Python ${v} (${py.cmd}), but the Cosmonapse CLI needs Python 3.11+.\n`,
        );
        break;
      }
    }
    return null;
  }

  process.stderr.write("cosmo: first run - installing the Cosmonapse CLI into a private environment (one-time)...\n");
  mkdirSync(path.dirname(VENV_DIR), { recursive: true });

  const venv = run(base.cmd, [...base.args, "-m", "venv", "--clear", VENV_DIR], { stdio: ["ignore", "ignore", "inherit"] });
  if (venv.status !== 0) {
    process.stderr.write(
      "cosmo: `python -m venv` failed. On Debian/Ubuntu you may need: apt install python3-venv\n",
    );
    return null;
  }

  const spec = version ? `cosmonapse==${version}` : "cosmonapse";
  let pip = run(venvPython(), ["-m", "pip", "install", "--quiet", spec], { stdio: ["ignore", "ignore", "inherit"] });
  if (pip.status !== 0 && version) {
    // The npm and PyPI releases are cut from the same tag, but tolerate a
    // publish gap: fall back to the latest PyPI release with a warning.
    process.stderr.write(`cosmo: ${spec} not installable; falling back to the latest cosmonapse release.\n`);
    pip = run(venvPython(), ["-m", "pip", "install", "--quiet", "cosmonapse"], { stdio: ["ignore", "ignore", "inherit"] });
  }
  if (pip.status !== 0) {
    process.stderr.write("cosmo: pip install failed (network or PyPI issue). See errors above.\n");
    return null;
  }

  if (version !== null) writeFileSync(STAMP, version + "\n", "utf8");
  return { cmd: venvPython(), args: [] };
}

// --- resolve and delegate ---------------------------------------------------

function resolve() {
  const version = pkgVersion();

  // 1. Explicit override.
  if (process.env.COSMO_PYTHON) {
    const py = { cmd: process.env.COSMO_PYTHON, args: [] };
    if (hasCosmo(py)) return py;
    process.stderr.write(
      `cosmo: $COSMO_PYTHON (${py.cmd}) cannot \`import cosmo\`. ` +
        "Install the CLI there with: pip install cosmonapse\n",
    );
    process.exit(127);
  }

  // 2. A system Python that already has the CLI (user-managed install).
  for (const py of interpreters()) {
    if (hasCosmo(py)) return py;
  }

  // 3. The managed venv (bootstrap or reuse).
  if (venvReady(version)) return { cmd: venvPython(), args: [] };
  const booted = bootstrap(version);
  if (booted) return booted;

  process.stderr.write(
    [
      "cosmo: no usable Python found.",
      "",
      "The `cosmo` CLI is implemented in the Python `cosmonapse` package, and",
      "this launcher installs it automatically - but it needs a Python 3.11+",
      "interpreter on PATH to do so.",
      "",
      "Install Python 3.11+ (https://www.python.org/downloads/) and re-run,",
      "or point $COSMO_PYTHON at an interpreter that has the CLI:",
      "",
      "    pip install cosmonapse",
      "    COSMO_PYTHON=$(which python3) cosmo --help",
      "",
    ].join("\n"),
  );
  process.exit(127);
}

const python = resolve();
const result = spawnSync(python.cmd, [...python.args, "-m", "cosmo", ...process.argv.slice(2)], {
  stdio: "inherit",
  windowsHide: true,
});
process.exit(result.status ?? (result.signal === "SIGINT" ? 130 : 1));
