"""factorial — compute n! for non-negative integers."""


def factorial(n: int) -> int:
    """Return n! (factorial) for n >= 0.

    Raises ValueError for negative inputs.
    """
    if n < 0:
        raise ValueError("factorial is undefined for negative integers")
    result = 1
    for i in range(2, n + 1):
        result *= i
    return result


if __name__ == "__main__":
    # 3 test cases: 0!, 1!, 5!
    assert factorial(0) == 1, "0! should be 1"
    assert factorial(1) == 1, "1! should be 1"
    assert factorial(5) == 120, "5! should be 120"

    # negative input should raise
    try:
        factorial(-1)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for negative input")

    print("factorial: all 3 test cases passed")
