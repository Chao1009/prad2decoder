"""
PMT Gain Response Model
=======================
Models the relationship between a HyCal PMT's high voltage and its
observed signal strength (the right edge of the Bremsstrahlung
spectrum) as a power law:

    edge = A * V^k

which is a straight line in log-log space:

    log(edge) = log(A) + k * log(V)

A :class:`PMTGainModel` instance accumulates ``(Vmon, edge)``
measurements for one module, fits the power law by ordinary least
squares in log-log space, and proposes a ΔV that brings the next
measurement to a target ADC value.

Selection rule for :meth:`PMTGainModel.delta_v_to_target`:

    * **≥ 2 points** — use the power-law fit.
    * **1 point**    — fall back to the static lookup table (the
      response slope is unknown so the step size is keyed to the
      magnitude of the ADC error).
    * **0 points**   — :class:`RuntimeError`.  This method must not
      be called before any measurement.

The class deliberately depends on nothing outside the standard library
so it can be unit-tested in isolation and reused from both the
gain-scan engine and any offline analysis or report-figure script.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple


# ----------------------------------------------------------------------
#  Power-law fit result
# ----------------------------------------------------------------------

@dataclass
class PMTFitResult:
    """Result of a power-law fit ``edge = A * V^k`` (linear in log-log).

    Attributes
    ----------
    a, k
        Power-law parameters in linear units.
    log_a
        Natural log of ``a`` (the intercept of the log-log line).
    n_points
        Number of measurements used in the fit.
    rss
        Residual sum of squares in log-log space.
    r_squared
        Coefficient of determination in log-log space.  Equals 1.0
        when ``n_points == 2`` (exact fit).
    """
    a: float
    k: float
    log_a: float
    n_points: int
    rss: float
    r_squared: float

    def predict_edge(self, vmon: float) -> float:
        """Edge ADC predicted by the fit at voltage ``vmon``."""
        return self.a * (vmon ** self.k)

    def predict_voltage(self, edge: float) -> float:
        """Voltage at which the fit predicts the given ``edge``."""
        return math.exp((math.log(edge) - self.log_a) / self.k)


# ----------------------------------------------------------------------
#  Lookup table (used when only one point is available)
# ----------------------------------------------------------------------

def lookup_delta_v(target_edge: float, current_edge: float) -> float:
    """Static voltage-step lookup table.

    The step magnitude is chosen from the absolute ADC error.  Sign
    follows the error: positive when ``current_edge`` is below the
    target (i.e. the PMT needs more gain).

    ===============  ===========
    \\|ADC diff\\|   Step
    ===============  ===========
    > 1000           50 V
    500 – 1000       30 V
    200 – 500        20 V
    100 – 200         5 V
    < 100            diff / 20 V
    ===============  ===========
    """
    diff = target_edge - current_edge
    sign = 1.0 if diff >= 0 else -1.0
    ad = abs(diff)
    if ad > 1000:
        dv = 50.0
    elif ad > 500:
        dv = 30.0
    elif ad > 200:
        dv = 20.0
    elif ad > 100:
        dv = 5.0
    else:
        dv = ad / 20.0
    return round(sign * dv, 1)


# ----------------------------------------------------------------------
#  PMT gain model
# ----------------------------------------------------------------------

class PMTGainModel:
    """Accumulate ``(Vmon, edge)`` measurements and propose ΔV.

    One instance models one PMT.  Call :meth:`clear` between modules
    or when restarting a module from scratch (``Redo``).

    A fit is considered usable only if it is **physical** (the
    log-log slope ``k`` is positive — gain rises with voltage) and of
    **good quality** (``R² >= r2_min``, applied when there are at
    least 3 points; for 2 points the line passes through both points
    and R² is meaningless).  When either check fails,
    :meth:`delta_v_to_target` falls back to the static lookup table
    on the most recent point.
    """

    #: Default minimum R² for a 3+ point fit to be trusted.
    DEFAULT_R2_MIN: float = 0.90

    def __init__(self, r2_min: float = DEFAULT_R2_MIN) -> None:
        self._points: List[Tuple[float, float]] = []
        self._fit: Optional[PMTFitResult] = None
        self.r2_min: float = r2_min

    # -- data point management ---------------------------------------------

    def add_point(self, vmon: float, edge: float) -> bool:
        """Record one ``(vmon, edge)`` measurement.

        Both must be strictly positive — the model lives in log space
        and silently ignores non-positive inputs to avoid math domain
        errors.  Returns True if the point was kept.
        """
        if vmon is None or edge is None:
            return False
        if vmon <= 0 or edge <= 0:
            return False
        if not (math.isfinite(vmon) and math.isfinite(edge)):
            return False
        self._points.append((float(vmon), float(edge)))
        self._fit = None  # invalidate cached fit
        return True

    def clear(self) -> None:
        """Forget all measurements and the cached fit."""
        self._points.clear()
        self._fit = None

    # -- read access -------------------------------------------------------

    @property
    def points(self) -> List[Tuple[float, float]]:
        """Recorded ``(vmon, edge)`` measurements (defensive copy)."""
        return list(self._points)

    @property
    def n_points(self) -> int:
        return len(self._points)

    @property
    def fit(self) -> Optional[PMTFitResult]:
        """The current fit, or None if fewer than 2 points are available.

        The fit is computed lazily by :meth:`linear_fit`; touching
        this property does **not** trigger a fit.  Use ``linear_fit``
        if you want one computed on demand.
        """
        return self._fit

    @property
    def latest(self) -> Optional[Tuple[float, float]]:
        """The most recent ``(vmon, edge)`` point, or None."""
        return self._points[-1] if self._points else None

    def is_good_fit(self, fit: Optional[PMTFitResult] = None) -> bool:
        """Return True if ``fit`` is physical and of acceptable quality.

        Pass ``fit`` explicitly, or omit to test the cached fit.

        * Returns False if there is no fit.
        * Returns False if the slope ``k`` is non-positive (gain must
          rise with voltage).
        * For ``n_points >= 3``, requires ``r_squared >= self.r2_min``.
          The R² check is skipped for ``n_points == 2`` because the
          fit is exact by construction and R² carries no information.
        """
        if fit is None:
            fit = self._fit
        if fit is None:
            return False
        if fit.k <= 0:
            return False
        if fit.n_points >= 3 and fit.r_squared < self.r2_min:
            return False
        return True

    # -- fitting -----------------------------------------------------------

    def linear_fit(self) -> Optional[PMTFitResult]:
        """Fit the power law to the recorded points.

        Performs ordinary least squares on ``(log V, log edge)``.
        Requires at least 2 points and a non-degenerate spread of
        voltages.  Returns the fit, or None if neither precondition
        is met.  Subsequent calls return the cached result until new
        points are added.
        """
        if self._fit is not None:
            return self._fit

        n = len(self._points)
        if n < 2:
            return None

        xs = [math.log(v) for v, _ in self._points]
        ys = [math.log(e) for _, e in self._points]
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        sxx = sum((x - mean_x) ** 2 for x in xs)
        if sxx <= 0:
            # all voltages identical — slope is undefined
            return None
        sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))

        k = sxy / sxx
        log_a = mean_y - k * mean_x

        rss = sum((y - (log_a + k * x)) ** 2 for x, y in zip(xs, ys))
        tss = sum((y - mean_y) ** 2 for y in ys)
        r2 = 1.0 - rss / tss if tss > 0 else 1.0

        self._fit = PMTFitResult(
            a=math.exp(log_a),
            k=k,
            log_a=log_a,
            n_points=n,
            rss=rss,
            r_squared=r2,
        )
        return self._fit

    # -- ΔV proposal -------------------------------------------------------

    def delta_v_to_target(self, target_edge: float) -> Tuple[float, str]:
        """Compute the voltage change needed to reach ``target_edge``.

        Returns ``(dv, mode_tag)``.  ``dv`` is rounded to 0.1 V to
        match HV-crate granularity, and ``mode_tag`` is a short
        human-readable string suitable for log output (which method
        was selected, what its parameters are, why a fallback was
        used, etc.).

        Selection:
            * ``n_points >= 2`` and a fit accepted by
              :meth:`is_good_fit` → use ``predict_voltage(target_edge)``
              and subtract the most recent ``vmon``.
            * ``n_points == 1`` → static lookup table on the ADC
              error of the single point.
            * ``n_points == 0`` → :class:`RuntimeError`.  Callers
              must add at least one point first.

        Multi-point fits are rejected (with a fallback to lookup) when
        they are degenerate (e.g. all voltages identical), give a
        non-physical slope (``k <= 0``), or — once there are 3+
        points — have ``r_squared < self.r2_min``.
        """
        n = len(self._points)
        if n == 0:
            raise RuntimeError(
                "PMTGainModel.delta_v_to_target called with no data points"
            )

        cur_vmon, cur_edge = self._points[-1]

        if n == 1:
            dv = lookup_delta_v(target_edge, cur_edge)
            return dv, "lookup (1 point)"

        fit = self.linear_fit()
        if fit is None:
            dv = lookup_delta_v(target_edge, cur_edge)
            return dv, f"lookup (degenerate fit, n={n})"
        if fit.k <= 0:
            dv = lookup_delta_v(target_edge, cur_edge)
            return dv, f"lookup (non-physical k={fit.k:.2f}, n={n})"
        if fit.n_points >= 3 and fit.r_squared < self.r2_min:
            dv = lookup_delta_v(target_edge, cur_edge)
            return dv, (f"lookup (poor fit R²={fit.r_squared:.3f} "
                        f"< {self.r2_min:.2f}, n={n})")

        v_target = fit.predict_voltage(target_edge)
        dv = round(v_target - cur_vmon, 1)
        return dv, (f"fit n={n} k={fit.k:.2f} "
                    f"A={fit.a:.3g} R²={fit.r_squared:.3f}")

    # -- convenience -------------------------------------------------------

    def __len__(self) -> int:
        return len(self._points)

    def __bool__(self) -> bool:
        return bool(self._points)

    def __repr__(self) -> str:
        if self._fit:
            return (f"PMTGainModel(n={len(self._points)}, "
                    f"k={self._fit.k:.3f}, A={self._fit.a:.3g})")
        return f"PMTGainModel(n={len(self._points)}, no fit)"
