from shop.pricing import apply_discount


class Cart:
    def __init__(self):
        self.items = []

    def add(self, name, price, qty=1):
        self.items.append((name, price, qty))

    def subtotal(self):
        return sum(p * q for _, p, q in self.items)

    def total(self, discount_percent=0):
        return apply_discount(self.subtotal(), discount_percent)
