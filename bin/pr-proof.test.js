// 입구의 작은 판단만 본다: 빠진 프로그램 안내, 쓰는 것을 두는 곳, 다시 설치할지의 표시,
// 안내에 쓸 명령 앞부분(부른 방식).
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
