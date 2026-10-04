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
