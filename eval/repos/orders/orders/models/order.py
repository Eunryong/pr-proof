from dataclasses import dataclass


@dataclass(frozen=True)
class Item:
    name: str
    unit_price: int
    quantity: int


@dataclass(frozen=True)
class Order:
    items: tuple[Item, ...]
    coupon_percent: int = 0
