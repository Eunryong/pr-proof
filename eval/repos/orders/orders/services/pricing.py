from orders.models.order import Order

FREE_SHIPPING_MIN = 50_000
SHIPPING_FEE = 3_000
MAX_COUPON_PERCENT = 50


def subtotal(order: Order) -> int:
    return sum(item.unit_price * item.quantity for item in order.items)


def discount(order: Order) -> int:
    if order.coupon_percent < 0 or order.coupon_percent > MAX_COUPON_PERCENT:
        raise ValueError("coupon_percent out of range")
    return subtotal(order) * order.coupon_percent // 100


def shipping_fee(order: Order) -> int:
    if not order.items:
        return 0
    if subtotal(order) - discount(order) >= FREE_SHIPPING_MIN:
        return 0
    return SHIPPING_FEE


def total(order: Order) -> int:
    return subtotal(order) - discount(order) + shipping_fee(order)
