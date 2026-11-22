### 4.2 체크아웃

| | |
| --- | --- |
| 입력 | `PRJob` (repo, PR 번호, head SHA, base SHA) |
| 출력 | worktree(경로는 그래프 상태에 넣지 않는다), merge-base, 테스트 전에 읽어 둔 PR diff, base의 설정 |
| 캐시 키 | 저장소별 bare clone |
| 실패 시 | git·gh 실패는 환경 오류로 재시도 |

지금 구현을 그대로 쓴다 (v1 F4).

- 저장소마다 한 번 `git clone --bare`, PR마다 `git fetch origin pull/<n>/head` 뒤 `git worktree add`.
- **바뀐 줄의 기준은 merge-base다.** `git merge-base base head`와 head의 diff(`git diff base...head`와 같다)를 쓴다. base가 앞서 나가도 main의 변경이 PR의 변경으로 잡히지 않는다. 규칙과 설정은 merge-base가 아니라 base 브랜치의 지금 커밋에서 읽는다.
- **worktree 안의 `.git`은 믿지 않는다.** `git worktree add` 직후, 그 안에서 아무것도 실행하기 전에 저장소 경로를 worktree 밖의 기록 파일에 적고, worktree에서 도는 git은 모두 그 경로를 `--git-dir`·`--work-tree`로 넘긴다. 기록이 없으면 git을 실행하지 않는다.
- **샌드박스가 한 번 돈 뒤의 worktree 파일은 믿지 않는다.** 호스트가 worktree 파일을 읽고 쓸 때는 심볼릭 링크를 따라가지 않고, 실제 위치가 worktree 밖이면 거부하고, 일반 파일만 크기 상한 안에서 읽는다. PR diff는 테스트 전에 읽어 메모리에 두고, 테스트 뒤에 바뀐 줄이 필요하면 git으로 다시 센다.
- 재개하면 worktree를 다시 만든다. 수정 묶음마다 같은 커밋으로 새 worktree를 쓴다(앞 수정의 변경이 섞이지 않게).

