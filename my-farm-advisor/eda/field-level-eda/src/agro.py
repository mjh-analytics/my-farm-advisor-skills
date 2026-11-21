"""Agronomic weather metrics from NASA POWER daily data (pipeline weather table).

Expected columns: field_id, lat, date, T2M_MAX, T2M_MIN, PRECTOTCORR (mm/day),
ALLSKY_SFC_SW_DWN (MJ m-2 day-1), RH2M (%), WS10M (m/s). Fill values (-999) become NaN.

* Growing degree days, corn method (deg F): Tmax and Tmin are each limited to 50-86 F,
  GDD = (Tmax + Tmin) / 2 - 50, never below 0.
* Reference evapotranspiration ET0: FAO-56 Penman-Monteith, daily (Allen et al. 1998,
  eqs. 7, 8, 11-13, 17, 19, 21, 37, 38, 39, 40, 47). Wind is reduced from 10 m to 2 m
  with eq. 47. This is the water use of a well-watered reference grass, not a specific
  crop; season water deficit = ET0 - rainfall.
"""
from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd

MM_PER_IN = 25.4
SIGMA = 4.903e-9  # MJ K-4 m-2 day-1


def c_to_f(c):
    return c * 9.0 / 5.0 + 32.0


def gdd_corn_f(tmax_f, tmin_f, base=50.0, cap=86.0):
    hi = np.clip(tmax_f, base, cap)
    lo = np.clip(tmin_f, base, cap)
    return np.maximum((hi + lo) / 2.0 - base, 0.0)


def _e0(t):
    return 0.6108 * np.exp(17.27 * t / (t + 237.3))


def et0_fao56(tmax, tmin, rs, rh_mean, u10, lat_deg, doy, elev_m):
    """Daily FAO-56 Penman-Monteith reference ET (mm/day). Temperatures in deg C."""
    tmean = (tmax + tmin) / 2.0
    p = 101.3 * ((293.0 - 0.0065 * elev_m) / 293.0) ** 5.26
    gamma = 0.000665 * p
    es = (_e0(tmax) + _e0(tmin)) / 2.0
    ea = rh_mean / 100.0 * es
    delta = 4098.0 * _e0(tmean) / (tmean + 237.3) ** 2
    u2 = u10 * 4.87 / np.log(67.8 * 10.0 - 5.42)
    phi = np.radians(lat_deg)
    dr = 1 + 0.033 * np.cos(2 * np.pi * doy / 365.0)
    dec = 0.409 * np.sin(2 * np.pi * doy / 365.0 - 1.39)
    ws = np.arccos(np.clip(-np.tan(phi) * np.tan(dec), -1, 1))
    ra = (24 * 60 / np.pi) * 0.0820 * dr * (ws * np.sin(phi) * np.sin(dec)
                                            + np.cos(phi) * np.cos(dec) * np.sin(ws))
    rso = (0.75 + 2e-5 * elev_m) * ra
    rns = 0.77 * rs
    ratio = np.clip(rs / np.where(rso > 0, rso, np.nan), 0.25, 1.0)
    rnl = SIGMA * ((tmax + 273.16) ** 4 + (tmin + 273.16) ** 4) / 2.0 \
        * (0.34 - 0.14 * np.sqrt(np.maximum(ea, 0))) * (1.35 * ratio - 0.35)
    rn = rns - rnl
    num = 0.408 * delta * rn + gamma * (900.0 / (tmean + 273.0)) * u2 * (es - ea)
    return np.maximum(num / (delta + gamma * (1 + 0.34 * u2)), 0.0)


def series_id(df: pd.DataFrame) -> str:
    """Fingerprint of a field's weather series; fields in the same NASA POWER grid cell
    share identical series, so this groups them into cells."""
    cols = ["date", "T2M_MAX", "T2M_MIN", "PRECTOTCORR"]
    b = df[cols].sort_values("date").round(4).to_csv(index=False).encode()
    return hashlib.sha256(b).hexdigest()[:12]


def daily_metrics(w: pd.DataFrame, elev_m: dict[str, float]) -> pd.DataFrame:
    w = w.replace(-999.0, np.nan).copy()
    w["date"] = pd.to_datetime(w["date"])
    w["year"] = w.date.dt.year
    w["doy"] = w.date.dt.dayofyear
    w["tmax_f"] = c_to_f(w.T2M_MAX)
    w["tmin_f"] = c_to_f(w.T2M_MIN)
    w["gdd_f"] = gdd_corn_f(w.tmax_f, w.tmin_f)
    w["rain_in"] = w.PRECTOTCORR / MM_PER_IN
    elev = w.field_id.map(elev_m).fillna(300.0)
    w["et0_in"] = et0_fao56(w.T2M_MAX, w.T2M_MIN, w.ALLSKY_SFC_SW_DWN, w.RH2M, w.WS10M,
                            w.lat, w.doy, elev) / MM_PER_IN
    return w


def in_season(d: pd.Series, start_md: tuple[int, int], end_md: tuple[int, int]) -> pd.Series:
    md = d.dt.month * 100 + d.dt.day
    return (md >= start_md[0] * 100 + start_md[1]) & (md <= end_md[0] * 100 + end_md[1])
