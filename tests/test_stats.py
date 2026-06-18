from finetunes.stats import crosspair_wins, wilson_interval


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


# --- crosspair_wins (Mann-Whitney U) --- #
def test_crosspair_wins_clean_sweep():
    # A all ranks higher (lower number) than B → A wins every pair.
    a_wins, total = crosspair_wins([1, 2, 3], [4, 5, 6])
    assert a_wins == 9
    assert total == 9


def test_crosspair_wins_zero_for_loser():
    a_wins, total = crosspair_wins([4, 5, 6], [1, 2, 3])
    assert a_wins == 0
    assert total == 9


def test_crosspair_wins_split():
    # A: ranks 1, 3, 5; B: 2, 4, 6 → A wins (1<2,4,6 = 3) + (3<4,6 = 2) + (5<6 = 1) = 6.
    a_wins, total = crosspair_wins([1, 3, 5], [2, 4, 6])
    assert a_wins == 6
    assert total == 9


def test_crosspair_wins_handles_empty():
    assert crosspair_wins([], [1, 2, 3]) == (0, 0)


def test_crosspair_wins_ties_count_half():
    a_wins, total = crosspair_wins([2], [2])
    assert a_wins == 0.5
    assert total == 1
