// 입구의 작은 판단만 본다: 빠진 프로그램 안내, 쓰는 것을 두는 곳, 다시 설치할지의 표시,
// 안내에 쓸 명령 앞부분(부른 방식), 새 판 알림(판 견주기, 볼 꼬리표, 알릴지, 하루 한 번)과 update.
// 레지스트리 조회와 npm install은 인자로 바꿔 끼워 실제로 부르지 않는다.
// 실행: npm test  (node --test bin/pr-proof.test.js)
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const test = require("node:test");

const entry = require("./pr-proof.js");

const HINTS = JSON.parse(
  fs.readFileSync(path.join(__dirname, "..", "app", "setup", "install_hints.json"), "utf8"),
);

test("빠진 프로그램을 한 번에 모두 운영체제에 맞게 보인다", () => {
  const text = entry.missingMessage(["python3", "gh", "claude"], HINTS, "darwin");
  assert.match(text, /python 3\.11 이상: brew install python@3\.12/);
  assert.match(text, /gh \(GitHub CLI\): brew install gh/);
  assert.match(text, /claude .*\/login/); // 깔기만 해서는 안 되고 로그인까지
  const linux = entry.missingMessage(["python3"], HINTS, "linux");
  assert.match(linux, /sudo apt install python3 python3-venv/);
});

test("모르는 운영체제는 공통 안내", () => {
  assert.equal(entry.platformKey("win32"), "other");
  for (const name of Object.keys(HINTS)) {
    for (const key of ["label", "darwin", "linux", "other"]) assert.ok(HINTS[name][key], name + key);
  }
});

test("clone한 폴더는 그 폴더의 .venv, 패키지는 사용자 폴더의 venv", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pr-proof-entry-"));
  const home = path.join(dir, "home");
  assert.equal(entry.venvDir(dir, { PR_PROOF_HOME: home }), path.join(home, "venv"));
  fs.mkdirSync(path.join(dir, ".git"));
  assert.equal(entry.venvDir(dir, { PR_PROOF_HOME: home }), path.join(dir, ".venv"));
  assert.equal(entry.venvDir(dir, { PR_PROOF_VENV: "/x/venv" }), "/x/venv");
});

test("설치 표시는 pyproject.toml이 바뀌면 달라진다", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pr-proof-stamp-"));
  fs.writeFileSync(path.join(dir, "package.json"), JSON.stringify({ version: "1.2.3" }));
  fs.writeFileSync(path.join(dir, "pyproject.toml"), "a");
  const first = entry.installStamp(dir);
  assert.match(first, / 1\.2\.3 /);
  fs.writeFileSync(path.join(dir, "pyproject.toml"), "b");
  assert.notEqual(entry.installStamp(dir), first);
});

test("모르는 명령은 사용법과 함께 2로 끝난다", () => {
  const result = spawnSync(process.execPath, [path.join(__dirname, "pr-proof.js"), "nope"], {
    encoding: "utf8",
  });
  assert.equal(result.status, 2);
  assert.match(result.stderr, /모르는 명령: nope/);
  assert.match(result.stderr, /add <repo>/);
});

// npx가 두는 환경 변수 (npm 11에서 실제로 부른 것과 같은 값)
const NPX = { npm_command: "exec", npm_lifecycle_event: "npx" };

test("전역 설치로 부르면 안내는 pr-proof", () => {
  const prefix = "/usr/local";
  const root = path.join(prefix, "lib", "node_modules", "pr-proof");
  assert.equal(entry.commandPrefix({}, root, "/home/u", path.join(prefix, "bin", "pr-proof")), "pr-proof");
});

test("npx로 부르면 npx 캐시든 전역 설치를 찾은 것이든 npx pr-proof", () => {
  const cache = "/home/u/.npm/_npx/a3fd392e4bebf832/node_modules/pr-proof";
  const bin = "/home/u/.npm/_npx/a3fd392e4bebf832/node_modules/.bin/pr-proof";
  assert.equal(entry.commandPrefix(NPX, cache, "/home/u", bin), "npx pr-proof");
  const global = "/usr/local/lib/node_modules/pr-proof";
  assert.equal(entry.commandPrefix({ npm_command: "exec" }, global, "/x", "/usr/local/bin/pr-proof"),
    "npx pr-proof");
});

test("clone한 폴더에서 npx .로 부르면 npx ., 다른 곳에서 부르면 그 폴더", () => {
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "pr-proof-clone-")));
  const bin = "/home/u/.npm/_npx/744614315fac2693/node_modules/.bin/pr-proof";
  assert.equal(entry.commandPrefix(NPX, dir, dir, bin), "npx .");
  assert.equal(entry.commandPrefix(NPX, dir, os.tmpdir(), bin), `npx ${dir}`);
});

test("node로 파일을 부르면 그 꼴", () => {
  const root = "/src/pr-proof";
  assert.equal(entry.commandPrefix({}, root, root, path.join(root, "bin", "pr-proof.js")),
    "node bin/pr-proof.js");
});

test("사용법은 부른 방식의 명령으로 보이고 model이 있다", () => {
  const text = entry.usage("pr-proof");
  assert.match(text, /^사용법: pr-proof <명령>/);
  assert.match(text, /model \[이름\]/);
  const result = spawnSync(process.execPath, [path.join(__dirname, "pr-proof.js"), "help"], {
    encoding: "utf8", env: { PATH: process.env.PATH },
  });
  assert.match(result.stdout, /^사용법: node .*pr-proof\.js <명령>/);
});

test("rag 명령이 있고 사용법에 보인다", () => {
  assert.match(entry.usage("pr-proof"), /rag \[on\|off\] +과거 수정 기록 검색/);
  assert.match(entry.usage("pr-proof"), /--model, --rag\)/);
});

test("실행 환경을 새로 깔면 [run] 다음에 켬일 때 [rag]도 까는 Python 명령을 부른다", () => {
  const [run, rag] = entry.installSteps("/pkg");
  assert.deepEqual(run, {
    args: ["-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--editable", "/pkg[run]"],
    required: true,
  });
  // 고른 값은 config.yaml에 있어 Python이 읽는다. 실패해도 본 명령은 이어 간다
  assert.deepEqual(rag, { args: ["-m", "app.setup.cli", "rag", "sync"], required: false });
});

// --- 새 판 알림과 update ---

test("판 견주기: 정식판끼리, 시험판끼리, 시험판과 정식판, 같은 판", () => {
  const cmp = entry.compareVersions;
  assert.equal(cmp("1.2.3", "1.2.4"), -1);
  assert.equal(cmp("1.10.0", "1.9.9"), 1); // 글자가 아니라 숫자로
  assert.equal(cmp("2.0.0", "1.99.99"), 1);
  assert.equal(cmp("0.1.2-beta.0", "0.1.3-beta.0"), -1);
  assert.equal(cmp("0.1.2-beta.10", "0.1.2-beta.9"), 1); // 숫자 조각은 숫자로
  assert.equal(cmp("1.0.0-alpha", "1.0.0-beta"), -1);
  assert.equal(cmp("1.0.0-alpha", "1.0.0-alpha.1"), -1); // 조각이 적은 쪽이 작다
  assert.equal(cmp("1.0.0-1", "1.0.0-alpha"), -1); // 숫자 조각 < 글자 조각
  assert.equal(cmp("1.0.0-rc.1", "1.0.0"), -1); // 시험판 < 같은 번호의 정식판
  assert.equal(cmp("1.0.0", "0.9.0-beta.0"), 1);
  assert.equal(cmp("0.1.2-beta.0", "0.1.2-beta.0"), 0);
  assert.equal(cmp("1.2.3", "1.2.3+build.5"), 0); // 빌드 꼬리는 보지 않는다
  assert.equal(cmp("nope", "1.0.0"), null);
});

test("볼 꼬리표: 시험판이면 next와 latest, 정식판이면 latest만", () => {
  assert.deepEqual(entry.tagsToCheck("0.1.2-beta.0"), ["next", "latest"]);
  assert.deepEqual(entry.tagsToCheck("1.0.0"), ["latest"]);
  const tags = { latest: "0.1.0-beta.0", next: "0.1.3-beta.0" };
  assert.deepEqual(entry.newerRelease("0.1.2-beta.0", tags), { tag: "next", version: "0.1.3-beta.0" });
  // latest가 정식판으로 옮겨 가면 시험판 사용자는 그쪽으로
  assert.deepEqual(entry.newerRelease("0.1.2-beta.0", { latest: "1.0.0", next: "0.1.3-beta.0" }),
    { tag: "latest", version: "1.0.0" });
  // 정식판 사용자에게 next의 시험판은 알리지 않는다
  assert.equal(entry.newerRelease("1.0.0", { latest: "1.0.0", next: "1.1.0-beta.0" }), null);
  assert.deepEqual(entry.newerRelease("1.0.0", { latest: "1.0.1" }), { tag: "latest", version: "1.0.1" });
  // 같은 판이나 더 낡은 판은 없음
  assert.equal(entry.newerRelease("0.1.2-beta.0", { latest: "0.1.0-beta.0", next: "0.1.2-beta.0" }), null);
  // 두 꼬리표가 같은 판이면 latest
  assert.deepEqual(entry.newerRelease("0.1.0-beta.0", { latest: "0.2.0-beta.0", next: "0.2.0-beta.0" }),
    { tag: "latest", version: "0.2.0-beta.0" });
  assert.equal(entry.newerRelease("0.1.0-beta.0", null), null);
  assert.equal(entry.newerRelease("0.1.0-beta.0", { latest: 3 }), null);
});

// 임시 폴더에 전역 설치의 꼴(<prefix>/lib/node_modules/pr-proof, <prefix>/bin/pr-proof)을 만든다
function fakeGlobal(version = "0.1.0-beta.0", { link = true } = {}) {
  const prefix = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "pr-proof-prefix-")));
  const root = path.join(prefix, "lib", "node_modules", "pr-proof");
  fs.mkdirSync(path.join(root, "bin"), { recursive: true });
  fs.writeFileSync(path.join(root, "bin", "pr-proof.js"), "");
  fs.writeFileSync(path.join(root, "package.json"), JSON.stringify({ version }));
  fs.mkdirSync(path.join(prefix, "bin"));
  if (link) fs.symlinkSync("../lib/node_modules/pr-proof/bin/pr-proof.js", path.join(prefix, "bin", "pr-proof"));
  return { prefix, root };
}

test("prefix 알아내기: 기본이 아닌 --prefix 설치도 그 자리, 아니면 null", () => {
  const { prefix, root } = fakeGlobal();
  assert.equal(entry.globalPrefix(root, "darwin"), prefix);
  assert.equal(entry.installKind(root, "darwin"), "global");
  const unlinked = fakeGlobal("0.1.0-beta.0", { link: false });
  assert.equal(entry.globalPrefix(unlinked.root, "darwin"), null); // bin이 이 패키지를 가리키지 않음
  const project = fs.mkdtempSync(path.join(os.tmpdir(), "pr-proof-project-"));
  const dep = path.join(project, "node_modules", "pr-proof");
  fs.mkdirSync(dep, { recursive: true });
  assert.equal(entry.globalPrefix(dep, "darwin"), null); // 다른 프로젝트의 의존성
  assert.equal(entry.installKind(dep, "darwin"), "other");
});

test("깔린 방식: npx 캐시, clone한 폴더", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "pr-proof-npx-"));
  const cache = path.join(home, ".npm", "_npx", "e1296116f7a42b8b", "node_modules", "pr-proof");
  fs.mkdirSync(cache, { recursive: true });
  assert.equal(entry.installKind(cache), "npx");
  const clone = fs.mkdtempSync(path.join(os.tmpdir(), "pr-proof-clone-"));
  fs.mkdirSync(path.join(clone, ".git"));
  assert.equal(entry.installKind(clone), "clone");
});

test("알릴지: 터미널 아님, 끔, CI, clone, 도움말이면 알리지 않는다", () => {
  const base = { command: "list", rest: [], env: {}, isTTY: true, kind: "global" };
  assert.equal(entry.noticeEnabled(base), true);
  assert.equal(entry.noticeEnabled({ ...base, kind: "npx" }), true);
  assert.equal(entry.noticeEnabled({ ...base, isTTY: false }), false);
  assert.equal(entry.noticeEnabled({ ...base, env: { PR_PROOF_NO_UPDATE_CHECK: "1" } }), false);
  assert.equal(entry.noticeEnabled({ ...base, env: { PR_PROOF_NO_UPDATE_CHECK: "0" } }), true);
  assert.equal(entry.noticeEnabled({ ...base, env: { CI: "true" } }), false);
  assert.equal(entry.noticeEnabled({ ...base, env: { CI: "false" } }), true);
  assert.equal(entry.noticeEnabled({ ...base, kind: "clone" }), false);
  assert.equal(entry.noticeEnabled({ ...base, kind: "other" }), false);
  assert.equal(entry.noticeEnabled({ ...base, rest: ["--help"] }), false);
  assert.equal(entry.noticeEnabled({ ...base, command: "add", rest: ["x/y", "-h"] }), false);
  assert.equal(entry.noticeEnabled({ ...base, command: "update" }), false); // update는 스스로 말한다
  assert.equal(entry.noticeEnabled({ ...base, command: "" }), false);
});

test("하루 한 번: 처음, 하루가 지남, 시계가 뒤로 감이면 다시 확인", () => {
  const now = Date.UTC(2026, 10, 23, 12);
  const day = 24 * 60 * 60 * 1000;
  assert.equal(entry.checkDue(null, now), true);
  assert.equal(entry.checkDue({}, now), true);
  assert.equal(entry.checkDue({ checkedAt: now - day + 1000 }, now), false);
  assert.equal(entry.checkDue({ checkedAt: now - day }, now), true);
  assert.equal(entry.checkDue({ checkedAt: now + 60000 }, now), true);
});

test("알림 줄은 부른 방식에 맞춘다: 전역이면 update, npx면 그 꼬리표로 다시 부르기", () => {
  const newer = { tag: "next", version: "0.1.3-beta.0" };
  assert.equal(entry.noticeLine("0.1.2-beta.0", newer, "global", "pr-proof", "list"),
    "새 판이 있습니다: 0.1.2-beta.0 → 0.1.3-beta.0   올리려면: pr-proof update");
  assert.equal(entry.noticeLine("0.1.0-beta.0", newer, "npx", "npx pr-proof", "list"),
    "새 판이 있습니다: 0.1.0-beta.0 → 0.1.3-beta.0   쓰려면: npx pr-proof@next list");
});

test("알림은 저장한 결과로 보이고, 확인할 때가 됐을 때만 뒤에서 새로 확인한다", () => {
  const { root } = fakeGlobal("0.1.2-beta.0");
  const env = { PR_PROOF_HOME: fs.mkdtempSync(path.join(os.tmpdir(), "pr-proof-home-")) };
  const now = Date.UTC(2026, 10, 23, 12);
  let refreshed = 0;
  const opts = { env, isTTY: true, root, now, refresh: () => refreshed++ };
  assert.equal(entry.prepareNotice("list", [], "pr-proof", opts), null); // 저장한 것이 없다
  assert.equal(refreshed, 1);
  entry.writeCache({ checkedAt: now - 1000, tags: { latest: "0.1.0-beta.0", next: "0.1.3-beta.0" } }, env);
  assert.match(entry.prepareNotice("list", [], "pr-proof", opts),
    /0\.1\.2-beta\.0 → 0\.1\.3-beta\.0 {3}올리려면: pr-proof update/);
  assert.equal(refreshed, 1); // 오늘 확인했다
  assert.equal(entry.prepareNotice("list", [], "pr-proof", { ...opts, isTTY: false }), null);
  // 저장 파일이 깨져도 조용히
  fs.writeFileSync(path.join(env.PR_PROOF_HOME, "update-check.json"), "{");
  assert.equal(entry.prepareNotice("list", [], "pr-proof", opts), null);
});

test("뒤의 확인이 실패해도 확인한 때를 적고 지난 꼬리표를 둔다", async () => {
  const env = { PR_PROOF_HOME: path.join(fs.mkdtempSync(path.join(os.tmpdir(), "pr-proof-home-")), "new") };
  await entry.refreshCache(env, async () => ({ latest: "1.0.0" }), () => 5);
  assert.deepEqual(entry.readCache(env), { checkedAt: 5, tags: { latest: "1.0.0" } });
  await entry.refreshCache(env, async () => { throw new Error("offline"); }, () => 9);
  assert.deepEqual(entry.readCache(env), { checkedAt: 9, tags: { latest: "1.0.0" } });
});

test("레지스트리 조회는 시간 상한을 넘으면 throw한다", async () => {
  const slow = (url, { signal }) => new Promise((resolve, reject) => {
    // Node 20의 AbortSignal.timeout은 타이머를 unref하므로, 실제 소켓처럼 이벤트 루프를 붙잡아 둔다
    const pending = setTimeout(() => resolve({ ok: true, json: async () => ({}) }), 5000);
    signal.addEventListener("abort", () => {
      clearTimeout(pending);
      reject(signal.reason);
    });
  });
  const started = Date.now();
  await assert.rejects(entry.fetchDistTags({}, 50, slow));
  assert.ok(Date.now() - started < 1000);
  let asked = "";
  const ok = async (url) => {
    asked = url;
    return { ok: true, json: async () => ({ latest: "1.0.0" }) };
  };
  assert.deepEqual(await entry.fetchDistTags({ npm_config_registry: "http://r.example/" }, 50, ok),
    { latest: "1.0.0" });
  assert.equal(asked, "http://r.example/-/package/pr-proof/dist-tags");
});

function updateDeps(overrides = {}) {
  const lines = { out: [], err: [] };
  const deps = {
    env: { PR_PROOF_HOME: fs.mkdtempSync(path.join(os.tmpdir(), "pr-proof-home-")) },
    prefix: "pr-proof",
    fetchTags: async () => ({ latest: "0.1.0-beta.0", next: "0.1.3-beta.0" }),
    npm: () => { throw new Error("npm을 부르면 안 된다"); },
    now: () => 1,
    out: (line) => lines.out.push(line),
    err: (line) => lines.err.push(line),
    ...overrides,
  };
  return { deps, lines };
}

test("update: 전역 설치면 그 prefix에 꼬리표의 새 판을 깔고 다시 띄우기와 재설치를 알린다", async () => {
  const { prefix, root } = fakeGlobal("0.1.2-beta.0");
  const calls = [];
  const npm = (args) => {
    calls.push(args);
    fs.writeFileSync(path.join(root, "package.json"), JSON.stringify({ version: "0.1.3-beta.0" }));
    return { status: 0 };
  };
  const { deps, lines } = updateDeps({ root, npm });
  assert.equal(await entry.updateCommand([], deps), 0);
  assert.deepEqual(calls, [["install", "-g", "--prefix", prefix, "pr-proof@next"]]);
  const text = lines.out.join("\n");
  assert.match(text, /올립니다: 0\.1\.2-beta\.0 → 0\.1\.3-beta\.0/);
  assert.match(text, /다 올렸습니다: 0\.1\.3-beta\.0/);
  assert.match(text, /서버\(pr-proof start\)가 돌고 있으면 다시 띄워야/);
  assert.match(text, /Python 실행 환경을 한 번 다시 설치/);
});

test("update: 이미 가장 새 판이면 깔지 않는다", async () => {
  const { root } = fakeGlobal("0.1.3-beta.0");
  const { deps, lines } = updateDeps({ root });
  assert.equal(await entry.updateCommand([], deps), 0);
  assert.match(lines.out.join("\n"), /가장 새 판입니다: 0\.1\.3-beta\.0/);
});

test("update: npm이 실패하면 손으로 칠 명령을 알리고 npm의 종료 코드로 끝난다", async () => {
  const { prefix, root } = fakeGlobal("0.1.2-beta.0");
  const { deps, lines } = updateDeps({ root, npm: () => ({ status: 243 }) });
  assert.equal(await entry.updateCommand([], deps), 243);
  assert.ok(lines.err.join("\n").includes(`손으로 올리려면: npm install -g --prefix ${prefix} pr-proof@next`));
});

test("update: 레지스트리를 못 읽으면 손으로 칠 명령만", async () => {
  const { root } = fakeGlobal("0.1.2-beta.0");
  const fetchTags = async () => { throw new Error("ENOTFOUND"); };
  const { deps, lines } = updateDeps({ root, fetchTags });
  assert.equal(await entry.updateCommand([], deps), 1);
  assert.match(lines.err.join("\n"), /확인하지 못했습니다 \(ENOTFOUND\)[\s\S]*npm install -g --prefix/);
});

test("update: npx면 깔지 않고 npx가 판을 고르는 방식을, clone이면 git pull을 말한다", async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "pr-proof-npx-"));
  const cache = path.join(home, "_npx", "abc", "node_modules", "pr-proof");
  fs.mkdirSync(cache, { recursive: true });
  fs.writeFileSync(path.join(cache, "package.json"), JSON.stringify({ version: "0.1.0-beta.0" }));
  const npx = updateDeps({ root: cache, prefix: "npx pr-proof" });
  assert.equal(await entry.updateCommand([], npx.deps), 0);
  assert.match(npx.lines.out.join("\n"), /npx pr-proof@next <명령>: 가장 새 시험판 next \(0\.1\.3-beta\.0\)/);
  const clone = fs.mkdtempSync(path.join(os.tmpdir(), "pr-proof-clone-"));
  fs.mkdirSync(path.join(clone, ".git"));
  fs.writeFileSync(path.join(clone, "package.json"), JSON.stringify({ version: "0.1.0-beta.0" }));
  const fetchTags = async () => { throw new Error("부르면 안 된다"); };
  const git = updateDeps({ root: clone, prefix: "npx .", fetchTags });
  assert.equal(await entry.updateCommand([], git.deps), 0);
  assert.match(git.lines.out.join("\n"), /git pull/);
});

test("update --check: 하루 한 번을 무시하고 지금 확인해 저장하고, 깔지 않는다", async () => {
  const { root } = fakeGlobal("0.1.0-beta.0");
  const { deps, lines } = updateDeps({ root, now: () => 42 });
  entry.writeCache({ checkedAt: 41, tags: { latest: "0.1.0-beta.0" } }, deps.env);
  assert.equal(await entry.updateCommand(["--check"], deps), 0);
  const text = lines.out.join("\n");
  assert.match(text, /지금 판: 0\.1\.0-beta\.0 \(npm 꼬리표: latest 0\.1\.0-beta\.0, next 0\.1\.3-beta\.0\)/);
  assert.match(text, /새 판이 있습니다: 0\.1\.0-beta\.0 → 0\.1\.3-beta\.0 {3}올리려면: pr-proof update/);
  assert.equal(entry.readCache(deps.env).tags.next, "0.1.3-beta.0");
  const bad = updateDeps({ root });
  assert.equal(await entry.updateCommand(["--nope"], bad.deps), 2);
});

test("update는 python 없이 입구에서 끝난다", () => {
  const result = spawnSync(process.execPath, [path.join(__dirname, "pr-proof.js"), "update", "--help"], {
    encoding: "utf8", env: { PATH: "/nonexistent" },
  });
  assert.equal(result.status, 0);
  assert.match(result.stdout, /update \[--check\]/);
  assert.match(entry.usage("pr-proof"), /update {13}새 판으로 올린다/);
});

test("npm 패키지에 프롬프트 파일이 모두 실린다", () => {
  // 빠지면 개발 폴더에서는 되고 설치한 곳에서만 워커가 시작하지 못한다
  const root = path.join(__dirname, "..");
  const worker = path.join(root, "app", "worker");
  const prompts = fs
    .readdirSync(worker, { recursive: true })
    .map((name) => String(name).split(path.sep).join("/"))
    .filter((name) => /(^|\/)prompts\/[^/]+\.md$/.test(name))
    .map((name) => `app/worker/${name}`);
  assert.ok(prompts.length > 10);
  const packed = spawnSync("npm", ["pack", "--dry-run", "--json"], { cwd: root, encoding: "utf8" });
  assert.equal(packed.status, 0, packed.stderr);
  const files = new Set(JSON.parse(packed.stdout)[0].files.map((f) => f.path));
  assert.deepEqual(prompts.filter((name) => !files.has(name)), []);
});
