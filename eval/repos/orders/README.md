# orders

파이프라인 평가용 예제 리포다. 주문 금액을 계산하는 작은 Python 프로젝트.

## 구조

```
orders/
├── models/     # 데이터 클래스. 다른 계층을 import하지 않는다
├── services/   # 비즈니스 로직. models만 import한다
└── api/        # 입출력 변환. services와 models를 import한다
tests/          # 테스트. 파일 이름은 test_<모듈>.py
```

의존 방향은 `api → services → models` 한쪽이다. 반대 방향 import는 금지한다.

## 테스트

```
pytest
```
