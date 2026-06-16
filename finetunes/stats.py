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
