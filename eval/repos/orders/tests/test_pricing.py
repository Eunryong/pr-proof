import pytest

from orders.models.order import Item, Order
from orders.services.pricing import discount, shipping_fee, subtotal, total


def order(*prices: int, coupon: int = 0) -> Order:
    return Order(items=tuple(Item("x", p, 1) for p in prices), coupon_percent=coupon)


def test_subtotal_sums_price_times_quantity():
    assert subtotal(Order(items=(Item("a", 1000, 2), Item("b", 500, 3)))) == 3500


def test_discount_applies_percent():
    assert discount(order(10_000, coupon=10)) == 1000


@pytest.mark.parametrize("percent", [-1, 51])
def test_discount_rejects_out_of_range(percent):
    with pytest.raises(ValueError):
        discount(order(10_000, coupon=percent))


def test_discount_accepts_boundaries():
    assert discount(order(10_000, coupon=0)) == 0
    assert discount(order(10_000, coupon=50)) == 5000


def test_shipping_fee_empty_order_is_free():
    assert shipping_fee(Order(items=())) == 0


def test_shipping_fee_boundary():
    assert shipping_fee(order(49_999)) == 3000
    assert shipping_fee(order(50_000)) == 0


def test_total():
    assert total(order(10_000, coupon=10)) == 12_000
