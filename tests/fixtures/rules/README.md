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

## 코드 규칙

이 저장소의 코드가 지키는 규칙이다. 규칙마다 한 문장이고, 필요한 곳에 지키는 예와 어기는 예를 붙였다.

### 의존 방향

- `orders/models`는 `orders/services`와 `orders/api`를 import하지 않는다.
- `orders/services`는 `orders/api`를 import하지 않는다.

### 출력

- `orders/` 아래 모듈은 `print`를 쓰지 않는다. 남길 것이 있으면 `logging`의 로거를 쓴다.

지키는 예:

```python
import logging

logger = logging.getLogger(__name__)


def discount(order):
    logger.debug("coupon_percent=%s", order.coupon_percent)
```

어기는 예:

```python
def discount(order):
    print("coupon_percent", order.coupon_percent)
```

### 상수

- 비교에 쓰는 기준값(금액, 비율의 상한)은 숫자를 직접 쓰지 않고 대문자 이름의 모듈 상수로 둔다. `0`, `1`, 퍼센트 계산의 `100`은 예외다.

지키는 예:

```python
MAX_COUPON_PERCENT = 50

if order.coupon_percent > MAX_COUPON_PERCENT:
    raise ValueError("coupon_percent out of range")
```

어기는 예:

```python
if order.coupon_percent > 50:
    raise ValueError("coupon_percent out of range")
```

- 함수는 모듈 수준 변수를 바꾸지 않는다. `global` 문을 쓰지 않는다.

### 금액

- 금액은 원 단위 정수(`int`)로 다룬다. `float`로 바꾸거나 `/`로 나누지 않고 `//`를 쓴다.

### 모델

- `orders/models`의 데이터 클래스는 `@dataclass(frozen=True)`로 선언한다.

지키는 예:

```python
@dataclass(frozen=True)
class Item:
    name: str
```

어기는 예:

```python
@dataclass
class Item:
    name: str
```

### 테스트

- 테스트 파일 이름은 `test_<모듈>.py`다.

## 테스트

```
pytest
```
