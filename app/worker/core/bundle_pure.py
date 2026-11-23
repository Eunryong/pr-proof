"""수정안 묶음의 이름 (docs/DESIGN.md 4.9, 3.1의 C7). 수정안 PR의 브랜치, 처리 기록, 결과 코멘트가
같이 쓴다. 값만 둔다."""

from typing import Final

# 기계적 수정 묶음 (3.1의 C7). 브랜치는 pipeline/pr-<n>-fix-mechanical
MECHANICAL_BUNDLE: Final = "mechanical"
# 모델 수정 묶음 (DESIGN.md 4.9, 3.1의 C7). 브랜치는 pipeline/pr-<n>-fix-model
MODEL_BUNDLE: Final = "model"
# 코멘트 수정안 묶음. 브랜치는 pipeline/pr-<n>-fix-feedback. `/fix`의 묶음 인자에는 없다
FEEDBACK_BUNDLE: Final = "feedback"
