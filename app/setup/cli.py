"""`python -m app.setup.cli <명령>`. 보통은 입구(`pr-proof <명령>`, bin/pr-proof.js)가 부른다.

- `configure`: 처음 설정 (`app/setup/configure.py`)
- `add <repo>...`, `rm <repo>...`, `list`: 처리할 repo 목록과 그 웹훅
- `model [이름 | default]`: 모든 단계에 쓸 모델 (config.yaml의 `model.default`)
- `start`: api, worker, (smee 주소가 있으면) smee-client를 띄운다 (`app/setup/start.py`)

`update`(새 판 깔기)와 새 판 알림은 Python이 필요 없어 입구(bin/pr-proof.js)에서 끝나고
여기로 오지 않는다.
"""

import argparse
import os
import sys
from typing import Final

from app.setup import configure as flow
from app.setup import files, github_setup, setup_pure, start

_REPO_HELP: Final = f"repo (예: {setup_pure.REPO_EXAMPLE})"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=files.command_prefix())
    commands = parser.add_subparsers(dest="command", required=True)

    conf = commands.add_parser(
        "configure", help="처음 설정",
        description="처음 설정. 터미널이면 묻고, 아니면 인자로만 받는다. 여러 번 돌려도 같다. "
        f"토큰은 화면에 보이지 않게 입력하거나 환경 변수 {github_setup.TOKEN_INPUT_ENV}로 준다 "
        "(명령줄 인자로는 받지 않는다).",
    )  # fmt: skip
    conf.add_argument("--repo", action="append", default=[], metavar="REPO",
                      help=f"처리할 {_REPO_HELP}. 여러 번 줄 수 있다")  # fmt: skip
    conf.add_argument("--auth", choices=["gh", "token"], help="GitHub 인증 방식")
    conf.add_argument("--webhook-url", help="웹훅 받는 주소. 주지 않으면 smee.io 주소를 만든다")
    conf.add_argument("--port", type=int, help="API 포트 (기본 8765)")
    conf.add_argument("--manual-webhook", action="store_true",
                      help="웹훅을 API로 등록하지 않고 손으로 등록할 값만 보인다")  # fmt: skip
    conf.add_argument("--model", metavar="NAME",
                      help="모든 단계에 쓸 모델 (예: opus). default면 기본값으로. 주지 않으면 "
                      "터미널에서 묻고, 아니면 그대로 둔다")  # fmt: skip
    conf.add_argument("--dry-run", action="store_true",
                      help="파일과 GitHub에 쓰지 않고 할 일만 보인다")  # fmt: skip

    add = commands.add_parser(
        "add", help="처리할 repo를 더한다",
        description="허용 목록에 더하고, 권한을 확인하고, 웹훅을 등록한다.",
    )  # fmt: skip
    add.add_argument("repos", nargs="+", metavar="REPO", help=_REPO_HELP)
    add.add_argument("--webhook-url", help="웹훅 받는 주소 (주지 않으면 configure가 저장한 것)")
    add.add_argument("--manual-webhook", action="store_true",
                     help="웹훅을 API로 등록하지 않고 손으로 등록할 값만 보인다")  # fmt: skip
    add.add_argument("--dry-run", action="store_true", help="쓰지 않고 할 일만 보인다")

    rm = commands.add_parser(
        "rm", help="처리할 repo를 뺀다",
        description=f"허용 목록에서 빼고, 저장한 웹훅 받는 주소의 웹훅만 지운다. {flow.LEFT_ALONE}",
    )  # fmt: skip
    rm.add_argument("repos", nargs="+", metavar="REPO", help=_REPO_HELP)
    rm.add_argument("--yes", action="store_true", help="확인을 묻지 않는다")
    rm.add_argument("--dry-run", action="store_true", help="쓰지 않고 할 일만 보인다")

    ls = commands.add_parser("list", help="처리하는 repo 목록 (설정 파일만 읽는다)")
    ls.add_argument(
        "--check", action="store_true", help="repo마다 웹훅이 맞게 등록됐는지 GitHub에서 본다"
    )

    mdl = commands.add_parser(
        "model", help="모든 단계에 쓸 모델을 보거나 바꾼다",
        description="이름 없이: 지금 설정과 단계별로 쓰일 모델을 보인다. 이름: 모든 단계를 그 "
        "모델로 (config.yaml의 model.default). default: 그 값을 지워 기본값으로. 저장소의 "
        ".pipeline.yaml에 단계별로 적은 값이 이 값보다 앞선다.",
    )  # fmt: skip
    mdl.add_argument("name", nargs="?", metavar="NAME",
                     help=f"모델 이름 (예: {setup_pure.MODEL_EXAMPLES}) 또는 default")  # fmt: skip

    run = commands.add_parser("start", help="서버 실행 (api, worker, smee에서 받아 오기)")
    run.add_argument("--node", help="입구가 넘기는 node 실행 파일")
    run.add_argument("--smee", help="입구가 넘기는 smee-client 실행 파일")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "start":
        return start.start(args.node, args.smee)
    io = flow.Io(interactive=sys.stdin.isatty() and sys.stdout.isatty())
    if args.command == "model":
        return flow.model(args.name, io)
    deps = flow.real_deps(os.environ)
    if args.command == "configure":
        opts = flow.Options(repos=args.repo, auth=args.auth, webhook_url=args.webhook_url,
                            port=args.port, dry_run=args.dry_run,
                            manual_webhook=args.manual_webhook, model=args.model)  # fmt: skip
        return flow.configure(opts, io, deps)
    if args.command == "add":
        opts = flow.Options(repos=args.repos, webhook_url=args.webhook_url, dry_run=args.dry_run,
                            manual_webhook=args.manual_webhook)  # fmt: skip
        return flow.add(opts, io, deps)
    if args.command == "rm":
        return flow.remove(flow.Options(repos=args.repos, yes=args.yes, dry_run=args.dry_run),
                           io, deps)  # fmt: skip
    return flow.list_repos(flow.Options(check=args.check), io, deps)


if __name__ == "__main__":
    sys.exit(main())
