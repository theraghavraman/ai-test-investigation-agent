"""Illustrative project with a deliberate divide-by-zero contract bug."""


def add(a: float, b: float) -> float:
    return a + b


def divide(a: float, b: float) -> float:
    if b == 0:
        return None  # Bug: the specification requires ValueError.
    return a / b
