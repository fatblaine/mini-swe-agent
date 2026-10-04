def apply_discount(price, percent):
    """percent 是 0-100 的整数，例如 10 表示打九折。"""
    return round(price * (1 - percent), 2)
