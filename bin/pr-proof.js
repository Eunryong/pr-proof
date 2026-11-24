#!/usr/bin/env node
// pr-proof의 입구. 인자를 받아 Python 쪽(app/setup/cli.py)을 부르기만 한다.
// 하는 일: python 3.11 이상 찾기 → 실행 환경(venv) 준비 → start면 smee-client 위치 찾기 → Python 호출.
// 실제 일(설정 파일, 인증, 웹훅 등록, 확인, 서버 띄우기)은 테스트할 수 있는 Python이 한다.
// 새 판 알림과 `update`는 Python이 필요 없어 여기서 끝낸다.
"use strict";

const { spawn, spawnSync } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..");
const PACKAGE = "pr-proof";
const COMMANDS = ["configure", "start", "add", "rm", "list", "model", "rag", "update"];
const PYTHONS = ["python3.14", "python3.13", "python3.12", "python3.11", "python3"];
const OTHER_PROGRAMS = ["docker", "git", "gh", "claude"];
const STAMP = ".pr-proof-install";
const UPDATE_CACHE = "update-check.json";
const DAY_MS = 24 * 60 * 60 * 1000;
const NOTICE_TIMEOUT_MS = 2000; // 뒤에서 하는 하루 한 번의 확인
const UPDATE_TIMEOUT_MS = 10000; // 사용자가 친 update와 update --check
const DEFAULT_REGISTRY = "https://registry.npmjs.org";

function usage(prefix) {
  return `사용법: ${prefix} <명령> [인자]

  configure          처음 설정. 묻는 말에 답한다 (질문 없이: --repo, --auth, --webhook-url, --port, --model, --rag)
  start              서버 실행 (api, worker, smee에서 받아 오기)
  add <repo>...      처리할 repo를 더한다 (예: octocat/hello-world)
  rm <repo>...       처리할 repo를 뺀다
  list               처리하는 repo 목록 (--check: 웹훅까지 확인)
  model [이름]       모든 단계에 쓸 모델. 이름 없이: 지금 설정 보기, default: 기본값으로
  rag [on|off]       과거 수정 기록 검색을 켜고 끈다. 인자 없이: 지금 상태 보기
  update             새 판으로 올린다 (--check: 깔지 않고 새 판이 있는지만 본다)

명령마다 --help로 자세한 인자를 본다.`;
}

function updateUsage(prefix) {
  return `사용법: ${prefix} update [--check]

npm에 더 새 판이 있으면 깐다(npm install -g). 쓰는 판이 시험판이면 꼬리표 next와 latest 중
더 새 것을, 정식판이면 latest를 본다.

  --check            깔지 않고 새 판이 있는지만 지금 확인한다

npx로 부르면 깔지 않는다(npx는 부를 때마다 꼬리표의 판을 받는다). clone한 폴더는 git pull로 받는다.
새 판 알림은 하루에 한 번 확인하고 PR_PROOF_NO_UPDATE_CHECK=1(또는 CI)로 끈다.`;
}

function realPath(p) {
  try {
    return fs.realpathSync(p);
  } catch {
    return path.resolve(p);
  }
}

// 사용자가 이 입구를 부른 방식. 화면의 안내(사용법, "먼저 … configure", "다음: … start")가 같은
// 방식으로 나오게 Python에 PR_PROOF_COMMAND로 넘긴다.
// - npx로 부르면 npm이 npm_command=exec, npm_lifecycle_event=npx를 둔다. 패키지가 node_modules 아래
//   (npx 캐시 ~/.npm/_npx/…, 또는 전역 설치를 npx가 찾은 것)면 `npx pr-proof`, clone한 폴더처럼
//   그 밖의 폴더면 그 폴더에서 불렀을 때 `npx .`, 다른 곳에서 불렀으면 `npx <폴더>`
// - 그 밖: 전역 설치(npm install -g, npm link)의 bin 이름 `pr-proof`로 불렸으면 `pr-proof`,
//   `node bin/pr-proof.js`처럼 파일로 불렀으면 그 꼴
function commandPrefix(env = process.env, root = ROOT, cwd = process.cwd(), script = process.argv[1] || "") {
  if (env.npm_command === "exec" || env.npm_lifecycle_event === "npx") {
    const dir = realPath(root);
    if (dir.split(path.sep).includes("node_modules")) return "npx pr-proof";
    return dir === realPath(cwd) ? "npx ." : `npx ${dir}`;
  }
  if (!script || path.basename(script) === "pr-proof") return "pr-proof";
  return `node ${path.relative(cwd, script) || script}`;
}

function platformKey(platform = process.platform) {
  return platform === "darwin" || platform === "linux" ? platform : "other";
}

function installHints() {
  return JSON.parse(fs.readFileSync(path.join(ROOT, "app", "setup", "install_hints.json"), "utf8"));
}

// 빠진 프로그램을 한 번에 모두 보이는 글
function missingMessage(missing, hints, platform = process.platform) {
  const key = platformKey(platform);
  const lines = ["필요한 프로그램이 없습니다. 설치한 뒤 다시 실행하세요.", ""];
  for (const name of missing) {
    const hint = hints[name];
    lines.push(`- ${hint.label}: ${hint[key]}`);
  }
  return lines.join("\n");
}

function onPath(name, envPath = process.env.PATH || "") {
  return envPath.split(path.delimiter).some((dir) => {
    if (!dir) return false;
    try {
      fs.accessSync(path.join(dir, name), fs.constants.X_OK);
      return true;
    } catch {
      return false;
    }
  });
}

function findPython() {
  const probe =
    "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)";
  for (const name of PYTHONS) {
    if (!onPath(name)) continue;
    const result = spawnSync(name, ["-c", probe], { stdio: "ignore" });
    if (result.status === 0) return name;
  }
  return null;
}

// 쓰는 것을 두는 곳. clone한 폴더(.git이 있음)면 그 폴더의 .venv(scripts/start.sh와 같이 쓴다),
// 아니면(npm 패키지, npx 캐시) 사용자 폴더. 패키지 폴더는 npx 캐시라 지워지거나 판마다 바뀐다.
function runtimeHome(env = process.env) {
  return env.PR_PROOF_HOME || path.join(os.homedir(), ".cache", "pipeline", "runtime");
}

function venvDir(root = ROOT, env = process.env) {
  if (env.PR_PROOF_VENV) return env.PR_PROOF_VENV;
  if (fs.existsSync(path.join(root, ".git"))) return path.join(root, ".venv");
  return path.join(runtimeHome(env), "venv");
}

// 설치를 다시 할지 가르는 표시: 패키지 위치, 판, pyproject.toml 내용
function installStamp(root = ROOT) {
  const pkg = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"));
  const pyproject = fs.readFileSync(path.join(root, "pyproject.toml"));
  const digest = crypto.createHash("sha256").update(pyproject).digest("hex").slice(0, 16);
  return `${fs.realpathSync(root)} ${pkg.version} ${digest}`;
}

function packageVersion(root = ROOT) {
  return JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8")).version;
}

// --- 판 견주기 (semver 순서. 의존성 없이 필요한 만큼만) ---

const VERSION_RE = /^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$/;

function parseVersion(text) {
  const m = VERSION_RE.exec(String(text || "").trim());
  if (!m) return null;
  return { core: [Number(m[1]), Number(m[2]), Number(m[3])], pre: m[4] ? m[4].split(".") : [] };
}

function isPrerelease(text) {
  const v = parseVersion(text);
  return v !== null && v.pre.length > 0;
}

// a가 b보다 작으면 -1, 같으면 0, 크면 1. 판의 꼴이 아니면 null.
// 시험판 꼬리: 꼬리 없는 판이 더 크다. 꼬리끼리는 점으로 나눈 조각을 차례로, 숫자는 숫자로,
// 숫자 조각은 글자 조각보다 작고, 앞이 모두 같으면 조각이 적은 쪽이 작다 (semver 11절).
function compareVersions(a, b) {
  const x = parseVersion(a);
  const y = parseVersion(b);
  if (x === null || y === null) return null;
  for (let i = 0; i < 3; i++) {
    if (x.core[i] !== y.core[i]) return x.core[i] < y.core[i] ? -1 : 1;
  }
  if (!x.pre.length || !y.pre.length) {
    if (x.pre.length === y.pre.length) return 0;
    return x.pre.length ? -1 : 1;
  }
  const numeric = (s) => /^\d+$/.test(s);
  for (let i = 0; i < Math.min(x.pre.length, y.pre.length); i++) {
    const p = x.pre[i];
    const q = y.pre[i];
    if (p === q) continue;
    if (numeric(p) && numeric(q)) return Number(p) < Number(q) ? -1 : 1;
    if (numeric(p) !== numeric(q)) return numeric(p) ? -1 : 1;
    return p < q ? -1 : 1;
  }
  if (x.pre.length === y.pre.length) return 0;
  return x.pre.length < y.pre.length ? -1 : 1;
}

// 볼 꼬리표: 시험판을 쓰고 있으면 next와 latest, 정식판이면 latest만
function tagsToCheck(current) {
  return isPrerelease(current) ? ["next", "latest"] : ["latest"];
}

// 볼 꼬리표 중 지금 판보다 새 것 가운데 가장 새 것 { tag, version }, 없으면 null.
// 두 꼬리표가 같은 판이면 latest를 고른다.
function newerRelease(current, distTags) {
  if (!distTags || typeof distTags !== "object") return null;
  let best = null;
  for (const tag of tagsToCheck(current).slice().reverse()) {
    const version = distTags[tag];
    if (typeof version !== "string" || compareVersions(version, current) !== 1) continue;
    if (best === null || compareVersions(version, best.version) === 1) best = { tag, version };
  }
  return best;
}

// --- 어떻게 깔렸나 ---

// 전역 설치(npm install -g [--prefix P])면 그 prefix. 패키지가 <P>/lib/node_modules/pr-proof에 있고
// <P>/bin/pr-proof가 이 패키지의 입구를 가리킬 때만 그렇다고 본다. 아니면 null.
function globalPrefix(root = ROOT, platform = process.platform) {
  const dir = realPath(root);
  const parent = path.dirname(dir);
  if (path.basename(parent) !== "node_modules") return null;
  let prefix;
  let bin;
  if (platform === "win32") {
    prefix = path.dirname(parent);
    bin = path.join(prefix, `${PACKAGE}.cmd`);
    return fs.existsSync(bin) ? prefix : null;
  }
  if (path.basename(path.dirname(parent)) !== "lib") return null;
  prefix = path.dirname(path.dirname(parent));
  bin = path.join(prefix, "bin", PACKAGE);
  try {
    return fs.realpathSync(bin) === realPath(path.join(dir, "bin", "pr-proof.js")) ? prefix : null;
  } catch {
    return null;
  }
}

// npx: npx 캐시(~/.npm/_npx/<해시>/node_modules/pr-proof)에서 돈다. npx는 부를 때마다 꼬리표를
//   레지스트리에서 풀어 그 판을 받는다(`npx pr-proof`는 latest, `npx pr-proof@next`는 next).
// global: 전역 설치. npx로 불렀어도 전역 설치가 있으면 npx는 그것을 그대로 실행하므로 여기에 든다.
// clone: git으로 받은 폴더(`npx .`, `node bin/pr-proof.js`, npm link).
// other: 그 밖(다른 프로젝트의 의존성 등). 어떻게 올릴지 모른다.
function installKind(root = ROOT, platform = process.platform) {
  const dir = realPath(root);
  const parts = dir.split(path.sep);
  if (parts.includes("_npx") && parts.includes("node_modules")) return "npx";
  if (globalPrefix(dir, platform) !== null) return "global";
  if (fs.existsSync(path.join(dir, ".git"))) return "clone";
  return "other";
}

// --- 새 판 알림 ---

function envOn(value) {
  if (value === undefined || value === null) return false;
  const v = String(value).trim().toLowerCase();
  return v !== "" && v !== "0" && v !== "false";
}

// 알림을 보일지. 터미널(stderr)일 때, 끄지 않았을 때, 전역 설치나 npx일 때, 도움말이 아닐 때만.
function noticeEnabled({ command, rest = [], env = process.env, isTTY = false, kind }) {
  if (!isTTY) return false;
  if (envOn(env.PR_PROOF_NO_UPDATE_CHECK) || envOn(env.CI)) return false;
  if (kind !== "global" && kind !== "npx") return false;
  if (!command || command === "update" || !COMMANDS.includes(command)) return false;
  if (rest.includes("--help") || rest.includes("-h")) return false;
  return true;
}

// 하루 한 번: 확인한 적이 없거나, 마지막 확인이 하루 전이거나, 시계가 뒤로 갔으면 다시 확인한다
function checkDue(cache, now = Date.now()) {
  if (!cache || typeof cache.checkedAt !== "number") return true;
  return now - cache.checkedAt >= DAY_MS || now < cache.checkedAt;
}

function cachePath(env = process.env) {
  return path.join(runtimeHome(env), UPDATE_CACHE);
}

function readCache(env = process.env) {
  try {
    const data = JSON.parse(fs.readFileSync(cachePath(env), "utf8"));
    return data && typeof data === "object" ? data : null;
  } catch {
    return null;
  }
}

function writeCache(data, env = process.env) {
  const file = cachePath(env);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(data) + "\n");
  fs.renameSync(tmp, file);
}

function registryUrl(env = process.env) {
  return (env.npm_config_registry || DEFAULT_REGISTRY).replace(/\/+$/, "");
}

// 레지스트리의 꼬리표 { latest, next, … }. 시간 상한을 넘거나 실패하면 throw.
async function fetchDistTags(env = process.env, timeoutMs = NOTICE_TIMEOUT_MS, fetchImpl = fetch) {
  const url = `${registryUrl(env)}/-/package/${PACKAGE}/dist-tags`;
  let response;
  try {
    response = await fetchImpl(url, {
      signal: AbortSignal.timeout(timeoutMs),
      headers: { accept: "application/json" },
    });
  } catch (error) {
    const timedOut = error && (error.name === "TimeoutError" || error.name === "AbortError");
    const cause = error && error.cause ? error.cause.code || error.cause.message : "";
    const why = timedOut ? `${timeoutMs / 1000}초 안에 답이 없음` : cause || (error && error.message) || String(error);
    throw new Error(`${url}: ${why}`);
  }
  if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
  const data = await response.json();
  if (!data || typeof data !== "object" || typeof data.latest !== "string") {
    throw new Error(`${url}: 꼬리표가 없습니다`);
  }
  return data;
}

function tagsText(distTags) {
  return ["latest", "next"]
    .filter((tag) => typeof distTags[tag] === "string")
    .map((tag) => `${tag} ${distTags[tag]}`)
    .join(", ");
}

// 새 판을 쓰는 방법. 부른 방식(prefix)과 깔린 방식(kind)에 맞춘다.
function upgradeHint(kind, prefix, tag, command) {
  if (kind === "global") return `올리려면: ${prefix} update`;
  if (kind === "npx") return `쓰려면: npx ${PACKAGE}@${tag} ${command || "<명령>"}`;
  if (kind === "clone") return "받으려면: git pull";
  return `전역으로 깔려면: npm install -g ${PACKAGE}@${tag}`;
}

function noticeLine(current, newer, kind, prefix, command) {
  return `새 판이 있습니다: ${current} → ${newer.version}   ${upgradeHint(kind, prefix, newer.tag, command)}`;
}

// 뒤에서 꼬리표를 받아 저장한다. 본 명령은 기다리지 않는다. 실패해도 확인한 때는 적어
// (지난 꼬리표는 그대로 두고) 하루 동안 다시 묻지 않는다.
async function refreshCache(env = process.env, fetchTags = fetchDistTags, now = Date.now) {
  const previous = readCache(env);
  let tags = previous && previous.tags && typeof previous.tags === "object" ? previous.tags : {};
  try {
    tags = await fetchTags(env, NOTICE_TIMEOUT_MS);
  } catch {}
  try {
    writeCache({ checkedAt: now(), tags }, env);
  } catch {}
}

function spawnRefresh() {
  const child = spawn(
    process.execPath,
    ["-e", `require(${JSON.stringify(__filename)}).refreshCache().finally(() => process.exit(0))`],
    { detached: true, stdio: "ignore", env: process.env },
  );
  child.on("error", () => {});
  child.unref();
}

// 이번 실행에 보일 알림 한 줄(없으면 null). 확인할 때가 됐으면 뒤에서 새로 확인한다.
// 어떤 실패도 본 명령에 닿지 않게 모두 삼킨다.
function prepareNotice(command, rest, prefix, {
  env = process.env,
  isTTY = Boolean(process.stderr.isTTY),
  root = ROOT,
  now = Date.now(),
  refresh = spawnRefresh,
} = {}) {
  try {
    const kind = installKind(root);
    if (!noticeEnabled({ command, rest, env, isTTY, kind })) return null;
    const cache = readCache(env);
    if (checkDue(cache, now)) refresh();
    const current = packageVersion(root);
    const newer = cache ? newerRelease(current, cache.tags) : null;
    return newer ? noticeLine(current, newer, kind, prefix, command) : null;
  } catch {
    return null;
  }
}

// --- update 명령 ---

function npmInstallArgs(prefix, tag) {
  return ["install", "-g", "--prefix", prefix, `${PACKAGE}@${tag}`];
}

function runNpm(args) {
  const npm = process.platform === "win32" ? "npm.cmd" : "npm";
  const result = spawnSync(npm, args, { stdio: "inherit" });
  if (result.error) return { status: 1, error: result.error.message };
  return { status: result.status === null ? 1 : result.status };
}

async function updateCommand(rest, {
  env = process.env,
  root = ROOT,
  prefix = commandPrefix(),
  fetchTags = fetchDistTags,
  npm = runNpm,
  readVersion = packageVersion,
  now = Date.now,
  out = (line) => console.log(line),
  err = (line) => console.error(line),
} = {}) {
  if (rest.includes("--help") || rest.includes("-h")) {
    out(updateUsage(prefix));
    return 0;
  }
  const unknown = rest.filter((arg) => arg !== "--check");
  if (unknown.length) {
    err(`pr-proof: update가 모르는 인자: ${unknown.join(" ")}\n\n${updateUsage(prefix)}`);
    return 2;
  }
  const check = rest.includes("--check");
  const kind = installKind(root);
  const current = readVersion(root);
  if (kind === "clone" && !check) {
    out(`clone한 폴더(${realPath(root)})는 git으로 받습니다: git pull`);
    return 0;
  }

  let distTags = null;
  let failure = "";
  try {
    distTags = await fetchTags(env, UPDATE_TIMEOUT_MS);
    try {
      writeCache({ checkedAt: now(), tags: distTags }, env);
    } catch {}
  } catch (error) {
    failure = error && error.message ? error.message : String(error);
  }
  const newer = distTags ? newerRelease(current, distTags) : null;
  const fallbackTag = isPrerelease(current) ? "next" : "latest";
  const tag = newer ? newer.tag : fallbackTag;

  if (check) {
    if (!distTags) {
      err(`pr-proof: npm에서 판을 확인하지 못했습니다 (${failure})`);
      return 1;
    }
    out(`지금 판: ${current} (npm 꼬리표: ${tagsText(distTags)})`);
    out(newer ? noticeLine(current, newer, kind, prefix, "") : "가장 새 판입니다.");
    return 0;
  }

  if (kind === "npx") {
    out(`npx는 부를 때마다 npm에서 꼬리표의 판을 받습니다. 깔 것이 없습니다 (지금 판: ${current}).`);
    const at = (name) => (distTags && distTags[name] ? ` (${distTags[name]})` : "");
    out(`- npx ${PACKAGE} <명령>: latest${at("latest")}. 다만 전역 설치가 있으면 npx는 그것을 실행합니다`);
    out(`- npx ${PACKAGE}@next <명령>: 가장 새 시험판 next${at("next")}`);
    return 0;
  }

  const globalAt = kind === "global" ? globalPrefix(root) : null;
  if (globalAt === null) {
    err(`pr-proof: 이 pr-proof(${realPath(root)})는 전역 설치(npm install -g)가 아니어서 올리지 않습니다.`);
    err(`깐 방법으로 다시 까거나, 전역으로 쓰려면: npm install -g ${PACKAGE}@${tag}`);
    return 1;
  }
  const manual = `npm install -g --prefix ${globalAt} ${PACKAGE}@${tag}`;
  if (!distTags) {
    err(`pr-proof: npm에서 판을 확인하지 못했습니다 (${failure})`);
    err(`손으로 올리려면: ${manual}`);
    return 1;
  }
  if (!newer) {
    out(`가장 새 판입니다: ${current} (npm 꼬리표: ${tagsText(distTags)})`);
    return 0;
  }
  out(`pr-proof를 올립니다: ${current} → ${newer.version} (${manual})`);
  const result = npm(npmInstallArgs(globalAt, newer.tag));
  if (result.status !== 0) {
    err(`pr-proof: npm이 실패했습니다 (${result.error || `종료 코드 ${result.status}`}). 손으로 올리려면: ${manual}`);
    return result.status || 1;
  }
  let installed = "";
  try {
    installed = readVersion(root);
  } catch {}
  if (installed !== newer.version) {
    err(`pr-proof: npm은 끝났지만 ${globalAt}에 깔린 판이 ${installed || "(읽지 못함)"}입니다. 손으로: ${manual}`);
    return 1;
  }
  out(`다 올렸습니다: ${installed}`);
  out(`서버(${prefix} start)가 돌고 있으면 다시 띄워야 새 판으로 돕니다.`);
  out("다음 실행에서 Python 실행 환경을 한 번 다시 설치합니다 (몇 분 걸릴 수 있다).");
  return 0;
}

function run(cmd, args, options = {}) {
  const result = spawnSync(cmd, args, { stdio: "inherit", ...options });
  if (result.status !== 0) {
    console.error(`pr-proof: 실패했습니다: ${cmd} ${args.join(" ")}`);
    process.exit(result.status || 1);
  }
}

// 실행 환경을 새로 깔 때 차례로 부르는 것. 둘째는 과거 수정 기록 검색을 켠 컴퓨터(config.yaml의
// rag.allowed: true)면 선택 의존성 [rag]도 깐다. 고른 값은 YAML이라 Python이 읽는다. 실패해도 본 명령은
// 이어 간다(검색 없이 검사하고, 결과 코멘트가 켜는 방법을 알린다).
function installSteps(root = ROOT) {
  return [
    // 편집 가능 설치: 코드가 패키지 폴더에 그대로 있어야 docker/sandbox 같은 파일을 찾는다
    { args: ["-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--editable", `${root}[run]`],
      required: true },
    { args: ["-m", "app.setup.cli", "rag", "sync"], required: false },
  ];
}

function ensureVenv(python, prefix) {
  const venv = venvDir();
  const venvPython = path.join(venv, "bin", "python");
  const stampPath = path.join(venv, STAMP);
  const stamp = installStamp();
  let current = "";
  try {
    current = fs.readFileSync(stampPath, "utf8").trim();
  } catch {}
  if (current === stamp && fs.existsSync(venvPython)) return venvPython;
  if (!fs.existsSync(venvPython)) {
    console.error(`pr-proof: 실행 환경을 만듭니다: ${venv}`);
    fs.mkdirSync(path.dirname(venv), { recursive: true });
    run(python, ["-m", "venv", venv]);
  }
  console.error("pr-proof: Python 패키지를 설치합니다 (처음 한 번, 몇 분 걸릴 수 있다)");
  const env = { ...process.env, PR_PROOF_COMMAND: prefix };
  for (const step of installSteps()) {
    if (step.required) run(venvPython, step.args);
    else spawnSync(venvPython, step.args, { stdio: "inherit", env });
  }
  fs.writeFileSync(stampPath, stamp + "\n");
  return venvPython;
}

// smee-client의 실행 파일. clone한 폴더에서 npm install을 하지 않았으면 여기서 한다
// (사용자가 칠 명령을 늘리지 않는다). npx로 받은 패키지에는 이미 깔려 있다.
function smeeBin() {
  const find = () => require.resolve("smee-client/bin/smee.js", { paths: [ROOT] });
  try {
    return find();
  } catch {}
  console.error("pr-proof: smee-client를 설치합니다 (npm ci)");
  const npm = process.platform === "win32" ? "npm.cmd" : "npm";
  run(npm, ["ci", "--omit=dev", "--no-audit", "--no-fund"], { cwd: ROOT });
  return find();
}

function main(argv) {
  const [command, ...rest] = argv;
  const prefix = commandPrefix();
  if (!command || command === "help" || command === "--help" || command === "-h") {
    console.log(usage(prefix));
    return 0;
  }
  if (!COMMANDS.includes(command)) {
    console.error(`pr-proof: 모르는 명령: ${command}\n\n${usage(prefix)}`);
    return 2;
  }
  if (command === "update") return updateCommand(rest, { prefix });
  // 알림은 본 명령의 출력을 깨지 않게 stderr에, 끝난 뒤에 보인다. 끝나지 않는 start는 시작할 때.
  const notice = prepareNotice(command, rest, prefix);
  if (notice && command === "start") console.error(notice);
  const python = findPython();
  if (python === null) {
    const missing = ["python3", ...OTHER_PROGRAMS.filter((name) => !onPath(name))];
    console.error(missingMessage(missing, installHints()));
    return 1;
  }
  const venvPython = ensureVenv(python, prefix);
  const args = ["-m", "app.setup.cli", command, ...rest];
  if (command === "start" && !rest.includes("--help") && !rest.includes("-h")) {
    args.push("--node", process.execPath, "--smee", smeeBin());
  }
  // PR_PROOF_NODE: configure가 smee 주소를 만들 때 쓰는 node (이 node)
  // PR_PROOF_COMMAND: 안내에 쓸 명령 앞부분 (app/setup/files.py의 command_prefix)
  const child = spawn(venvPython, args, {
    stdio: "inherit",
    env: { ...process.env, PR_PROOF_NODE: process.execPath, PR_PROOF_COMMAND: prefix },
  });
  for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"]) {
    process.on(signal, () => child.kill(signal));
  }
  child.on("exit", (code, signal) => {
    if (notice && command !== "start") console.error(notice);
    process.exit(code === null ? 1 : code);
  });
  return null;
}

module.exports = {
  missingMessage, platformKey, venvDir, installStamp, runtimeHome, commandPrefix, usage,
  parseVersion, isPrerelease, compareVersions, tagsToCheck, newerRelease, globalPrefix, installKind,
  noticeEnabled, checkDue, readCache, writeCache, fetchDistTags, noticeLine, refreshCache,
  prepareNotice, updateCommand, npmInstallArgs, installSteps,
};

if (require.main === module) {
  Promise.resolve(main(process.argv.slice(2))).then((code) => {
    if (code !== null) process.exit(code);
  });
}
