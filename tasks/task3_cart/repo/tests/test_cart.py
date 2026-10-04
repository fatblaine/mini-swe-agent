from shop.cart import Cart


def test_no_discount():
    c = Cart()
    c.add("apple", 2.0, 3)
    assert c.total() == 6.0


def test_ten_percent_off():
    c = Cart()
    c.add("book", 50.0, 2)
    assert c.total(10) == 90.0
