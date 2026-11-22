"""GARCH(1,1) baseline for next-day realised volatility (13.4).

The model is fitted on hourly log returns (in percent) once a month, on the last
FIT_DAYS days of data available at the month's first 00:00 UTC (a rolling window:
fitted on the whole history since 2020, the unconditional variance stays at the
level of 2020–2022 and biases later forecasts upwards). Within the month the parameters stay
fixed and the conditional variance is filtered forward hour by hour, so the
forecast made at day D uses returns up to the candle that closed at D 00:00 only.
Next-day volatility = sqrt(sum of the 24 one-hour-ahead variance forecasts).
"""

import logging
import warnings

import numpy as np
import pandas as pd
from arch import arch_model

logger = logging.getLogger(__name__)

HORIZON = 24
FIT_DAYS = 365


def hourly_returns_pct(close: pd.Series) -> pd.Series:
    """Hourly log returns x100, indexed by the candle's open time; gaps dropped."""
    lr = np.log(close / close.shift(1)) * 100
    return lr.dropna()


def _model(returns: pd.Series):
    return arch_model(returns, mean="Zero", vol="GARCH", p=1, q=1, dist="normal", rescale=False)


def garch_daily_vol(close: pd.Series, days: pd.DatetimeIndex) -> pd.Series:
    """Forecast of next-day realised volatility (fraction, not %) for each day D."""
    r = hourly_returns_pct(close.asfreq("1h"))
    out = pd.Series(np.nan, index=days, dtype=float)
    for month in pd.DatetimeIndex(sorted({d.replace(day=1) for d in days})):
        in_month = days[(days >= month) & (days < month + pd.offsets.MonthBegin(1))]
        # The candle opening at D-1h is the last one closed at D 00:00.
        last_known = month - pd.Timedelta(hours=1)
        fit_data = r[(r.index <= last_known) & (r.index > last_known - pd.Timedelta(days=FIT_DAYS))]
        if len(fit_data) < 24 * 90:
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            params = _model(fit_data).fit(disp="off").params
            last_origin = in_month[-1] - pd.Timedelta(hours=1)
            series = r[r.index <= last_origin]
            first = series.index.searchsorted(in_month[0] - pd.Timedelta(hours=25))
            fc = (
                _model(series)
                .fix(params)
                .forecast(horizon=HORIZON, start=series.index[first], reindex=False)
            )
        daily_var = fc.variance.sum(axis=1)
        for d in in_month:
            origin = daily_var.index.asof(d - pd.Timedelta(hours=1))
            if origin is not pd.NaT and not pd.isna(origin):
                out[d] = np.sqrt(daily_var[origin]) / 100
        logger.debug("GARCH %s: %s", month.date(), dict(params.round(4)))
    return out
