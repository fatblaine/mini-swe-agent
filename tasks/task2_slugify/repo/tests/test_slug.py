from textutil.slug import slugify


def test_simple():
    assert slugify("Hello World") == "hello-world"


def test_punctuation_and_spaces():
    assert slugify("  Hello,  World!! ") == "hello-world"


def test_numbers():
    assert slugify("Top 10 Tips") == "top-10-tips"
