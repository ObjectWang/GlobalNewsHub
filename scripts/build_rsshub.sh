#!/usr/bin/env bash
# scripts/build_rsshub.sh -- PRD task P2.1
#
# Packages RSSHub into standalone binaries per PRD section 4.1 (plan B:
# pkg-packaged standalone binary + subprocess management; no Docker or
# Node.js runtime dependency at runtime).
#
# Pipeline:
#   0. toolchain: Node >= 22 (RSSHub engines), portable fallback on Windows
#   1. clone RSSHub source (shallow, mirror fallback)
#   2. pnpm install           (browser downloads disabled: not needed, §1.2)
#   3. pnpm run build         (build:routes + tsdown -> dist/)
#   4. patch dist             (disable dev/test-only dynamic route imports)
#   5. build tools            (esbuild + @yao-pkg/pkg in a scratch dir)
#   6. esbuild                (single-file ESM bundle, minified)
#   7. stage assets           (wasm files + package.json bin/pkg.assets)
#   8. pkg --sea              (enhanced SEA mode, Node >= 22 targets)
#   9. publish                (resources/rsshub-server{.exe,-linux,-mac})
#
# Why ESM + SEA (validated 2026-08-24):
#   - RSSHub's entry uses top-level await; pkg's standard snapshot mode
#     cannot transform ESM-with-TLA to CJS.
#   - Node's module-syntax detector rejects sources mixing TLA with
#     require()/undefined CJS globals; the esbuild banner therefore
#     defines require/__filename/__dirname for bundled CJS modules.
#   - SEA mode ships the ESM entry as source inside the executable.
#   - wasm assets (simplecc, quickjs) are read relative to the entry and
#     must be staged next to the bundle (pkg.assets).
#
# Knobs (environment variables):
#   RSSHUB_REPO   git remote   (default: github.com/DIYgod/RSSHub)
#   RSSHUB_REF    branch/tag   (default: master; pin for reproducible builds)
#   NPM_REGISTRY  npm registry (default: npmmirror, fast in CN)
#   NODE_TARGET   pkg target   (default: node22, matching RSSHub engines)
#   PKG_TARGETS   comma list   (default: win-x64,linux-x64,macos-arm64)
#   BUILD_DIR     scratch dir  (default: <repo>/build/rsshub)
#
# Idempotent: existing clone / node_modules / toolchain are reused.
# Note: the macOS output needs an ad-hoc signature before distribution
# (run `codesign --sign - rsshub-server-mac` on a Mac).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${BUILD_DIR:-$REPO_ROOT/build/rsshub}"
SRC_DIR="$BUILD_DIR/src"
TOOLS_DIR="$BUILD_DIR/tools"
BUNDLE_DIR="$BUILD_DIR/bundle"
PKG_CACHE_DIR="$BUILD_DIR/pkg-cache"
RESOURCES_DIR="$REPO_ROOT/resources"

RSSHUB_REPO="${RSSHUB_REPO:-https://github.com/DIYgod/RSSHub.git}"
RSSHUB_REF="${RSSHUB_REF:-master}"
NPM_REGISTRY="${NPM_REGISTRY:-https://registry.npmmirror.com}"
NODE_TARGET="${NODE_TARGET:-node22}"
PKG_TARGETS="${PKG_TARGETS:-$NODE_TARGET-win-x64,$NODE_TARGET-linux-x64,$NODE_TARGET-macos-arm64}"
PORTABLE_NODE_VERSION="${PORTABLE_NODE_VERSION:-22.23.2}"

# Headless browsers are banned by PRD section 1.2; never download them.
export PUPPETEER_SKIP_DOWNLOAD=1
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export NODE_OPTIONS="--max-old-space-size=10240 --max-http-header-size=32768"
export COREPACK_NPM_REGISTRY="$NPM_REGISTRY"

log()  { echo "[build_rsshub] $*"; }
need() { command -v "$1" >/dev/null 2>&1 || { echo "missing tool: $1" >&2; exit 1; }; }

# --- 0a. Node toolchain (RSSHub engines: ^22.22.2 || ^24.15.0) --------------
need git
node_major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
if [ "$node_major" -lt 22 ]; then
  case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*)
      NODE_HOME="$BUILD_DIR/node-v$PORTABLE_NODE_VERSION-win-x64"
      if [ ! -f "$NODE_HOME/node.exe" ]; then
        need curl
        log "system node v$(node --version) too old; fetching portable Node v$PORTABLE_NODE_VERSION"
        mkdir -p "$BUILD_DIR"
        if [ ! -f "$BUILD_DIR/node.zip" ]; then
          curl -fSL --retry 3 \
            "$NPM_REGISTRY/-/binary/node/v$PORTABLE_NODE_VERSION/node-v$PORTABLE_NODE_VERSION-win-x64.zip" \
            -o "$BUILD_DIR/node.zip"
        fi
        rm -rf "$NODE_HOME"
        # GNU tar cannot read .zip; Expand-Archive can. Windows-only branch.
        powershell.exe -NoProfile -NonInteractive -Command \
          "Expand-Archive -LiteralPath '$(cygpath -w "$BUILD_DIR/node.zip")' -DestinationPath '$(cygpath -w "$BUILD_DIR")' -Force"
        rm -f "$BUILD_DIR/node.zip"
      fi
      export PATH="$NODE_HOME:$PATH"
      ;;
    *)
      echo "RSSHub requires Node ^22.22.2 || ^24.15.0; install Node 22+ first" >&2
      exit 1
      ;;
  esac
fi
need node
log "node $(node --version)"

# --- 0b. source -------------------------------------------------------------
if [ -d "$SRC_DIR/.git" ]; then
  log "source present, skipping clone ($SRC_DIR)"
else
  rm -rf "$SRC_DIR"
  mkdir -p "$BUILD_DIR"
  # GitHub connectivity is intermittent from some networks; try the direct
  # remote first, then known proxies/mirrors as fallback.
  CANDIDATES=("$RSSHUB_REPO")
  if [ "$RSSHUB_REPO" = "https://github.com/DIYgod/RSSHub.git" ]; then
    CANDIDATES+=("https://ghproxy.net/https://github.com/DIYgod/RSSHub.git")
  fi
  cloned=0
  for repo in "${CANDIDATES[@]}"; do
    log "cloning $repo@$RSSHUB_REF"
    if GIT_TERMINAL_PROMPT=0 git clone --depth 1 --branch "$RSSHUB_REF" "$repo" "$SRC_DIR"; then
      cloned=1
      break
    fi
    log "clone failed from $repo; trying next candidate"
  done
  [ "$cloned" = "1" ] || { echo "all clone sources failed" >&2; exit 1; }
fi

# --- 0c. pnpm (honor the repo's packageManager pin via corepack) ------------
cd "$SRC_DIR"
if grep -q '"packageManager": *"pnpm@' package.json; then
  need corepack
  PNPM=(corepack pnpm)
elif command -v pnpm >/dev/null 2>&1; then
  PNPM=(pnpm)
elif command -v pnpm.cmd >/dev/null 2>&1; then
  PNPM=(pnpm.cmd)
else
  echo "missing tool: pnpm" >&2
  exit 1
fi
log "pnpm $("${PNPM[@]}" --version 2>/dev/null || echo pinned-by-corepack)"

# --- 1. dependencies --------------------------------------------------------
if [ -f node_modules/.modules.yaml ]; then
  log "node_modules present, skipping pnpm install"
else
  log "pnpm install"
  "${PNPM[@]}" install --registry="$NPM_REGISTRY"
fi

# --- 2. upstream build (dist/) ----------------------------------------------
log "pnpm run build (build:routes + tsdown)"
"${PNPM[@]}" run build
[ -f dist/index.mjs ] || { echo "build produced no dist/index.mjs" >&2; exit 1; }

# --- 3. patch dist for bundling ----------------------------------------------
# The dev/test route registry imports routes through template-literal paths
# (./routes/${ns}/${loc}) that esbuild cannot resolve. Those branches only
# run when NODE_ENV != production (the bundle bakes NODE_ENV=production), so
# rejecting them is semantically safe.
log "patching dist (disable dev/test dynamic route imports)"
node - "$SRC_DIR/dist" <<'PATCH_EOF'
const fs = require("fs");
const path = require("path");
const dist = process.argv[2];
const re = /await import\(`\.\/routes\/\$\{[^}]+\}\/\$\{[^}]+\}`\)/g;
const replacement = 'await Promise.reject(new Error("dev/test route registry is disabled in packaged builds"))';
let count = 0;
for (const f of fs.readdirSync(dist).filter((x) => x.endsWith(".mjs"))) {
  const p = path.join(dist, f);
  const s = fs.readFileSync(p, "utf8");
  if (re.test(s)) {
    re.lastIndex = 0;
    fs.writeFileSync(p, s.replace(re, replacement));
    count++;
  }
  re.lastIndex = 0;
}
console.log(`[build_rsshub] patched ${count} dist chunk(s)`);
PATCH_EOF

# --- 4. build tools (esbuild + pkg), kept out of the source tree ------------
if [ ! -f "$TOOLS_DIR/node_modules/.modules.yaml" ]; then
  log "installing build tools (esbuild, @yao-pkg/pkg)"
  rm -rf "$TOOLS_DIR"
  mkdir -p "$TOOLS_DIR"
  cd "$TOOLS_DIR"
  # Pin the toolchain pnpm version (corepack resolves per package.json) and
  # allow esbuild's postinstall: pnpm >= 10 blocks dependency build scripts
  # by default and exits non-zero, which would abort this build (set -e).
  # confirm-modules-purge keeps non-TTY reruns unattended.
  printf '{\n  "name": "tools",\n  "private": true,\n  "packageManager": "pnpm@10.34.5"\n}\n' > package.json
  printf 'only-built-dependencies[]=esbuild\nconfirm-modules-purge=false\n' > .npmrc
  "${PNPM[@]}" add -D esbuild @yao-pkg/pkg --registry="$NPM_REGISTRY"
else
  cd "$TOOLS_DIR"
fi

# --- 5. pre-seed pkg base binaries (GitHub is often blocked in CN) ----------
export PKG_CACHE_PATH="$PKG_CACHE_DIR"
log "ensuring pkg base binaries for: $PKG_TARGETS"
node - "$PKG_TARGETS" <<'SEEDEOF'
const { execFileSync } = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");
const targets = process.argv[2].split(",").map((t) => t.trim()).filter(Boolean);
const cacheRoot = process.env.PKG_CACHE_PATH;
// Locate the pkg-fetch shipped with @yao-pkg/pkg (namespaced fork).
const pnpmDir = path.join(process.cwd(), "node_modules", ".pnpm");
const pfDir = fs.readdirSync(pnpmDir).find((d) => d.startsWith("@yao-pkg+pkg-fetch@"));
if (!pfDir) { console.error("pkg-fetch not found"); process.exit(1); }
const shas = JSON.parse(fs.readFileSync(
  path.join(pnpmDir, pfDir, "node_modules", "@yao-pkg", "pkg-fetch", "lib-es5", "expected-shas.json"), "utf8"));
const tag = "v" + pfDir.slice(pfDir.lastIndexOf("@") + 1).split(".").slice(0, 2).join(".");
const mirrors = [
  (n) => `https://github.com/yao-pkg/pkg-fetch/releases/download/${tag}/${n}`,
  (n) => `https://ghproxy.net/https://github.com/yao-pkg/pkg-fetch/releases/download/${tag}/${n}`,
];
function sha256(file) {
  return crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");
}
for (const t of targets) {
  const [nodeRange, platform, arch] = [t.split("-")[0], t.split("-")[1], t.split("-").slice(2).join("-")];
  // node22 -> the actual version baked into this script's portable/toolchain
  const nodeVersion = "v" + process.versions.node;
  const asset = `node-${nodeVersion}-${platform}-${arch}`;
  const key = asset;
  const dest = path.join(cacheRoot, tag, `fetched-${nodeVersion}-${platform}-${arch}`);
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  if (fs.existsSync(dest) && sha256(dest) === shas[key]) { console.log(`[build_rsshub] base cached: ${asset}`); continue; }
  let ok = false;
  for (const mk of mirrors) {
    const url = mk(asset);
    console.log(`[build_rsshub] downloading ${url}`);
    try {
      execFileSync("curl", ["-fSL", "--retry", "2", "--connect-timeout", "20", url, "-o", `${dest}.tmp`], { stdio: "inherit" });
      if (shas[key] && sha256(`${dest}.tmp`) !== shas[key]) throw new Error("sha256 mismatch");
      fs.renameSync(`${dest}.tmp`, dest);
      ok = true; break;
    } catch (e) { console.error(`[build_rsshub] mirror failed: ${e.message}`); }
  }
  if (!ok) { console.error(`could not obtain pkg base binary ${asset}`); process.exit(1); }
}
SEEDEOF

# --- 6. ESM bundle ------------------------------------------------------------
mkdir -p "$BUNDLE_DIR"
log "esbuild bundle -> $BUNDLE_DIR/rsshub.mjs"
# Banner contract (do not remove): bundled CJS modules call require() and
# reference __dirname; Node's detector treats a TLA module containing
# undefined CJS globals as ambiguous, so all of them must be defined.
BANNER="import { createRequire as __cr } from 'node:module'; import { fileURLToPath as __furl } from 'node:url'; import { dirname as __dn } from 'node:path'; const require = __cr(import.meta.url); const __filename = __furl(import.meta.url); const __dirname = __dn(__filename);"
"${PNPM[@]}" exec esbuild "$SRC_DIR/dist/index.mjs" \
  --bundle --platform=node --format=esm --target="$NODE_TARGET" \
  --minify --define:process.env.NODE_ENV=\"production\" \
  --external:chromium-bidi \
  --banner:js="$BANNER" \
  --outfile="$BUNDLE_DIR/rsshub.mjs" --log-level=warning

# --- 7. stage assets -----------------------------------------------------------
# simplecc-wasm reads <__dirname>/simplecc_wasm_bg.wasm; RSSHub core reads
# ../quickjs.wasm relative to the entry; header-generator (got header
# spoofing) reads <__dirname>/data_files/*.json|zip. Stage all where the
# packaged entry will see them (bundle/ and its parent), and declare them
# as pkg assets so the SEA archive includes them.
log "staging wasm + data_files assets"
SIMPLECC_WASM="$(find -L "$SRC_DIR/node_modules" -path "*simplecc-wasm*/simplecc_wasm_bg.wasm" | head -n 1)"
QUICKJS_WASM="$(find -L "$SRC_DIR/node_modules" -path "*quickjs-wasi*/quickjs.wasm" | head -n 1)"
[ -n "$SIMPLECC_WASM" ] || { echo "simplecc_wasm_bg.wasm not found" >&2; exit 1; }
cp "$SIMPLECC_WASM" "$BUNDLE_DIR/simplecc_wasm_bg.wasm"
if [ -n "$QUICKJS_WASM" ]; then cp "$QUICKJS_WASM" "$BUILD_DIR/quickjs.wasm"; fi
HEADER_GEN_DATA_FILES="$(find -L "$SRC_DIR/node_modules" -path "*header-generator*/data_files" -type d | head -n 1)"
[ -n "$HEADER_GEN_DATA_FILES" ] || { echo "header-generator data_files not found" >&2; exit 1; }
mkdir -p "$BUNDLE_DIR/data_files"
cp -r "$HEADER_GEN_DATA_FILES"/. "$BUNDLE_DIR/data_files"/
cat > "$BUNDLE_DIR/package.json" <<'PKGJSON_EOF'
{
  "name": "rsshub-bundle",
  "version": "1.0.0",
  "type": "module",
  "private": true,
  "bin": { "rsshub": "./rsshub.mjs" },
  "pkg": {
    "assets": [
      "./simplecc_wasm_bg.wasm",
      "../quickjs.wasm",
      "./data_files/headers-order.json",
      "./data_files/browser-helper-file.json",
      "./data_files/header-network-definition.zip",
      "./data_files/input-network-definition.zip"
    ]
  }
}
PKGJSON_EOF

# --- 8. pkg -> standalone binaries (enhanced SEA mode) ------------------------
log "pkg targets: $PKG_TARGETS (SEA mode; base binaries in $PKG_CACHE_DIR)"
"${PNPM[@]}" exec pkg "$BUNDLE_DIR" \
  --sea --targets "$PKG_TARGETS" \
  --out-path "$BUNDLE_DIR" \
  --compress GZip

# --- 9. publish into resources/ -----------------------------------------------
mkdir -p "$RESOURCES_DIR"
copy_bin() {  # $1 suffix glob, $2 destination
  local src
  src="$(find "$BUNDLE_DIR" -maxdepth 1 -name "rsshub-*$1" | head -n 1)"
  [ -n "$src" ] || { echo "pkg output for *$1 not found in $BUNDLE_DIR" >&2; exit 1; }
  cp "$src" "$2"
  chmod +x "$2" 2>/dev/null || true
  log "published $2 ($(du -h "$2" | cut -f1))"
}
copy_bin "win-x64.exe" "$RESOURCES_DIR/rsshub-server.exe"
copy_bin "linux-x64"   "$RESOURCES_DIR/rsshub-server-linux"
copy_bin "macos-arm64" "$RESOURCES_DIR/rsshub-server-mac"

log "done"
