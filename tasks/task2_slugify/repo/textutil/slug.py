import re


def slugify(title):
    s = title.lower()
    s = re.sub(r"[^a-z0-9]", "-", s)
    return s
