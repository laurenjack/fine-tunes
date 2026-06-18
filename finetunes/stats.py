"""Win-rate statistics."""
import math


def wilson_interval(wins, n, z=1.96):
    """Wilson score confidence interval for a binomial proportion.

    Returns (point_estimate, ci_low, ci_high) as proportions in [0, 1].
    For n == 0 returns (0.0, 0.0, 0.0).
    """
    if n <= 0:
        return 0.0, 0.0, 0.0
    phat = wins / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (phat + z2 / (2 * n)) / denom
    margin = (z * math.sqrt(phat * (1 - phat) / n + z2 / (4 * n * n))) / denom
    return phat, max(0.0, center - margin), min(1.0, center + margin)


def crosspair_wins(ranks_a, ranks_b):
    """Count cross-pair wins for side A vs side B given two lists of ranks.

    Each (a, b) where a < b is one A win; a > b is one B win; a == b is a tie
    counted as half a win (Mann-Whitney convention). Returns (a_wins, total).
    Ties shouldn't happen here (ranks are a permutation of 1..N), but handle
    them defensively.
    """
    a_wins = 0.0
    total = len(ranks_a) * len(ranks_b)
    for a in ranks_a:
        for b in ranks_b:
            if a < b:
                a_wins += 1
            elif a == b:
                a_wins += 0.5
    return a_wins, total
