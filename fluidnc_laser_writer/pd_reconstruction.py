"""Display-only upper-cluster reconstruction; not a power/safety measurement."""

from __future__ import annotations
import math
import statistics
from .pd_controller import PDSample


def reconstruct_high_curve(samples: list[PDSample]) -> list[float]:
    """Reject separated low clusters in 200 ms blocks; bridge only <=200 ms gaps.

    Blocks with no distinct two-cluster structure retain their measured values.
    Trailing rejected points stay missing until a right-hand anchor is available.
    """
    values = [s.plot_value for s in samples]
    result = values.copy()
    start = 0
    while start < len(samples):
        end = start + 1
        while (
            end < len(samples)
            and samples[end].host_time_s - samples[start].host_time_s < 0.2
        ):
            end += 1
        ordered = sorted(values[start:end])
        if len(ordered) >= 6:
            split = max(
                range(2, len(ordered) - 1), key=lambda k: ordered[k] - ordered[k - 1]
            )
            low, high = (ordered[:split], ordered[split:])
            gap = high[0] - low[-1]
            low_spread = statistics.median(
                (abs(v - statistics.median(low)) for v in low)
            )
            if gap >= max(40.0, 6 * low_spread) and low[-1] - low[0] <= max(
                30.0, gap * 0.25
            ):
                threshold = (low[-1] + high[0]) / 2
                for i in range(start, end):
                    if values[i] < threshold:
                        result[i] = math.nan
        start = end
    left = None
    for right, value in enumerate(result):
        if not math.isfinite(value):
            continue
        if left is not None and right > left + 1:
            duration = samples[right].host_time_s - samples[left].host_time_s
            if 0 < duration <= 0.2:
                for i in range(left + 1, right):
                    fraction = (
                        samples[i].host_time_s - samples[left].host_time_s
                    ) / duration
                    result[i] = result[left] + fraction * (value - result[left])
        left = right
    return result
