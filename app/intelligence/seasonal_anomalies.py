"""Walk-forward STL innovations and persistent level-shift signals."""
import numpy as np
from statsmodels.tsa.seasonal import STL

from app.intelligence.forecast_metrics import seasonal_period


def seasonal_signals(points, frequency):
    step = 7 if frequency == 'weekly' else 1
    first = 0
    values = np.asarray([point.value for point in points])
    for index in range(1, len(points)):
        if (points[index].date - points[index - 1].date).days != step:
            first = index
    previous_shift = 0
    for index in range(max(first + 12, len(points) - 40), len(points)):
        # The two preceding values are also held out for shift confirmation.
        window_start = max(first, index - 2 - 156)
        past = values[window_start:index - 2]
        period = seasonal_period(past, frequency)
        if period > 1:
            fit = STL(past, period=period, robust=True).fit()
            trend, remainder = fit.trend, fit.resid
            seasonal = np.resize(fit.seasonal[-period:], 3)
        else:
            trend = np.asarray([np.median(past[max(0, i-7):i+1]) for i in range(len(past))])
            remainder = past - trend
            seasonal = np.zeros(3)
        slope = np.median(np.diff(trend[-8:]))
        residual_center = np.median(remainder)
        reference = trend[-1] + slope * np.arange(1, 4) + seasonal + residual_center
        mad = np.median(np.abs(remainder - residual_center))
        scale = max(1.4826 * mad, np.median(np.abs(past)) * 0.01, 1e-9)
        z = (values[index-2:index+1] - reference) / scale
        shift = int(np.sign(z[0])) if (np.all(z >= 3.5) or np.all(z <= -3.5)) else 0
        if shift and shift != previous_shift:
            yield index, float(reference[-1]), float(min(abs(z))), len(past), 'persistent_level_shift'
        elif not shift and abs(z[-1]) >= 4.5:
            yield index, float(reference[-1]), float(abs(z[-1])), len(past), 'robust_innovation'
        previous_shift = shift
