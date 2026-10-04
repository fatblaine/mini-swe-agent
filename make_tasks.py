"""生成 3 个小 repo，每个埋一个让测试失败的 bug。运行：python make_tasks.py"""
import json
import shutil
import textwrap
from pathlib import Path

TASKS = {
    # 难度 1：单文件、报错直接指向 bug
    "task1_median": {
        "issue": "stats.median 对偶数长度的列表返回了错误结果，测试失败了。请修复源码，不要改测试。",
        "files": {
            "stats.py": """
                def mean(xs):
                    if not xs:
                        raise ValueError("empty")
                    return sum(xs) / len(xs)


                def median(xs):
                    if not xs:
                        raise ValueError("empty")
                    s = sorted(xs)
                    mid = len(s) // 2
                    return s[mid]
            """,
            "tests/test_stats.py": """
                import pytest
                from stats import mean, median


                def test_mean():
                    assert mean([1, 2, 3]) == 2


                def test_median_odd():
                    assert median([3, 1, 2]) == 2


                def test_median_even():
                    assert median([4, 1, 3, 2]) == 2.5


                def test_median_empty():
                    with pytest.raises(ValueError):
                        median([])
            """,
        },
    },
    # 难度 2：需要理解字符串处理细节，多个断言
    "task2_slugify": {
        "issue": "slugify 生成的 URL slug 不符合预期（多余的连字符、首尾没清理干净）。测试失败了，请修复。",
        "files": {
            "textutil/__init__.py": "",
            "textutil/slug.py": """
                import re


                def slugify(title):
                    s = title.lower()
                    s = re.sub(r"[^a-z0-9]", "-", s)
                    return s
            """,
            "tests/test_slug.py": """
                from textutil.slug import slugify


                def test_simple():
                    assert slugify("Hello World") == "hello-world"


                def test_punctuation_and_spaces():
                    assert slugify("  Hello,  World!! ") == "hello-world"


                def test_numbers():
                    assert slugify("Top 10 Tips") == "top-10-tips"
            """,
        },
    },
    # 难度 3：测试在 cart，bug 在 pricing —— 必须跨文件追踪
    "task3_cart": {
        "issue": "购物车打折后的总价不对，test_cart 失败。请找到根因并修复。",
        "files": {
            "shop/__init__.py": "",
            "shop/pricing.py": """
                def apply_discount(price, percent):
                    \"\"\"percent 是 0-100 的整数，例如 10 表示打九折。\"\"\"
                    return round(price * (1 - percent), 2)
            """,
            "shop/cart.py": """
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
            """,
            "tests/test_cart.py": """
                from shop.cart import Cart


                def test_no_discount():
                    c = Cart()
                    c.add("apple", 2.0, 3)
                    assert c.total() == 6.0


                def test_ten_percent_off():
                    c = Cart()
                    c.add("book", 50.0, 2)
                    assert c.total(10) == 90.0
            """,
        },
    },
}


def main(root="tasks"):
    root = Path(root)
    shutil.rmtree(root, ignore_errors=True)
    for name, spec in TASKS.items():
        repo = root / name / "repo"
        for rel, src in spec["files"].items():
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(textwrap.dedent(src).lstrip(), encoding="utf-8")
        (repo / "conftest.py").write_text("# 让 pytest 把仓库根目录加入 sys.path\n", encoding="utf-8")
        (root / name / "task.json").write_text(json.dumps({
            "issue": spec["issue"],
            "verify": "python -m pytest -q",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"created {root / name}")


if __name__ == "__main__":
    main()
