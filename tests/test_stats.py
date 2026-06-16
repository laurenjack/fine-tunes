from finetunes.stats import wilson_interval


def test_zero_n():
    assert wilson_interval(0, 0) == (0.0, 0.0, 0.0)


def test_point_estimate():
    point, low, high = wilson_interval(7, 10)
    assert point == 0.7
    assert 0.0 <= low < point < high <= 1.0


def test_known_wilson_values():
    # 50/100 -> ~0.5 with CI roughly [0.404, 0.596]
    point, low, high = wilson_interval(50, 100)
    assert point == 0.5
    assert abs(low - 0.404) < 0.01
    assert abs(high - 0.596) < 0.01


def test_bounds_clamped():
    point, low, high = wilson_interval(10, 10)
    assert point == 1.0
    assert high <= 1.0
    assert low >= 0.0
