#!/usr/bin/env node
// pr-proof의 입구. 인자를 받아 Python 쪽(app/setup/cli.py)을 부르기만 한다.
// 하는 일: python 3.11 이상 찾기 → 실행 환경(venv) 준비 → start면 smee-client 위치 찾기 → Python 호출.
// 실제 일(설정 파일, 인증, 웹훅 등록, 확인, 서버 띄우기)은 테스트할 수 있는 Python이 한다.
"use strict";

const { spawn, spawnSync } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..");
const COMMANDS = ["configure", "start", "add", "rm", "list", "model"];
const PYTHONS = ["python3.14", "python3.13", "python3.12", "python3.11", "python3"];
const OTHER_PROGRAMS = ["docker", "git", "gh", "claude"];
const STAMP = ".pr-proof-install";

function usage(prefix) {
  return `사용법: ${prefix} <명령> [인자]

  configure          처음 설정. 묻는 말에 답한다 (질문 없이: --repo, --auth, --webhook-url, --port, --model)
  start              서버 실행 (api, worker, smee에서 받아 오기)
  add <repo>...      처리할 repo를 더한다 (예: octocat/hello-world)
  rm <repo>...       처리할 repo를 뺀다
  list               처리하는 repo 목록 (--check: 웹훅까지 확인)
  model [이름]       모든 단계에 쓸 모델. 이름 없이: 지금 설정 보기, default: 기본값으로

명령마다 --help로 자세한 인자를 본다.`;
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

function run(cmd, args, options = {}) {
  const result = spawnSync(cmd, args, { stdio: "inherit", ...options });
  if (result.status !== 0) {
    console.error(`pr-proof: 실패했습니다: ${cmd} ${args.join(" ")}`);
    process.exit(result.status || 1);
  }
}

function ensureVenv(python) {
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
  // 편집 가능 설치: 코드가 패키지 폴더에 그대로 있어야 docker/sandbox 같은 파일을 찾는다
  run(venvPython, ["-m", "pip", "install", "--quiet", "--disable-pip-version-check",
    "--editable", `${ROOT}[run]`]);
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
  const python = findPython();
  if (python === null) {
    const missing = ["python3", ...OTHER_PROGRAMS.filter((name) => !onPath(name))];
    console.error(missingMessage(missing, installHints()));
    return 1;
  }
  const venvPython = ensureVenv(python);
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
  child.on("exit", (code, signal) => process.exit(code === null ? 1 : code));
  return null;
}

module.exports = { missingMessage, platformKey, venvDir, installStamp, runtimeHome, commandPrefix, usage };

if (require.main === module) {
  const code = main(process.argv.slice(2));
  if (code !== null) process.exit(code);
}
