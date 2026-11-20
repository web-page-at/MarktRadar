"""Forecast models for MarktRadar — Python standard library only.

Two separate questions are answered, because they behave very differently:

1. Direction ("will the price be higher in 5 trading days?")
   A pooled, strongly regularised logistic regression is trained on all stocks
   together (≈25 000 samples instead of ≈2 500 per stock) with trend,
   relative-strength, volatility and market-regime features (S&P 500, VIX).
   It is evaluated walk-forward (train only on the past, test the next half
   year, repeat). The model's probability is then blended with the stock's
   historical up-rate; the blend weight is whatever minimised the Brier score
   out-of-sample. If the model has no measurable edge, the weight is 0 and the
   probability is the honest base rate instead of a confident-looking guess.

2. Range ("how far can it move?")
   Volatility clusters, so this part is genuinely predictable. A HAR model
   (Corsi 2009) forecasts the next 5-day volatility from 1-, 5- and 22-day
   Parkinson range volatility, 60-day close volatility, an EWMA and the VIX.
   Empirical quantiles of past returns scaled by that forecast give an 80%
   range whose coverage is checked walk-forward.
"""
from __future__ import annotations

import math
from math import exp, log, sqrt, sumprod

HORIZON = 5
LOWER_Q, UPPER_Q = 0.10, 0.90  # 80% range
MODEL_VERSION = "2.1"
FEATURE_NAMES = [
    "ret_1d", "ret_5d", "ret_20d", "ret_60d", "ret_120d", "ret_252d",
    "rel_spy_5d", "rel_spy_20d", "vol_ratio", "vol_level",
    "rsi14", "dist_sma50", "dist_sma200", "stoch20", "drawdown_252d",
    "volume_z", "atr_ratio", "gap", "vix_level", "vix_change_5d",
    "spy_ret_20d", "spy_dist_sma200",
]
WARMUP = 252


def _ffill(days: list[int], source: dict[int, float]) -> list[float | None]:
    """Value of `source` on each day, carrying the last known value forward."""
    keys = sorted(source)
    out, k, last = [], 0, None
    for d in days:
        while k < len(keys) and keys[k] <= d:
            last = source[keys[k]]
            k += 1
        out.append(last)
    return out


def _clean(x: float) -> float:
    return x if math.isfinite(x) else 0.0


def positive(s: dict) -> dict:
    """Copy of a price series where non-positive prices (e.g. WTI oil at -37 $ in April 2020)
    are replaced by the last positive close, so log returns stay defined."""
    out = {k: list(v) for k, v in s.items()}
    last = next((x for x in s["c"] if x and x > 0), 1.0)
    for i in range(len(out["c"])):
        if not out["c"][i] or out["c"][i] <= 0:
            out["c"][i] = last
        last = out["c"][i]
        for k in ("o", "h", "l"):
            if k in out and (not out[k][i] or out[k][i] <= 0):
                out[k][i] = out["c"][i]
    return out


def direction_features(s: dict, spy: dict[int, float], vix: dict[int, float]) -> list[list[float] | None]:
    """Feature vector for every bar (None during warm-up). `s` holds lists day/o/h/l/c/v."""
    s = positive(s)
    days, c, hi, lo, op, vol = s["day"], s["c"], s["h"], s["l"], s["o"], s["v"]
    sc, vx = _ffill(days, spy), _ffill(days, vix)
    n = len(c)
    lr = [0.0] + [log(c[i] / c[i - 1]) if c[i] > 0 and c[i - 1] > 0 else 0.0 for i in range(1, n)]
    out: list[list[float] | None] = []
    for i in range(n):
        if i < WARMUP or sc[i] is None or sc[i - 252] is None or not vx[i] or not vx[i - 5]:
            out.append(None)
            continue
        w = lr[i - 59:i + 1]
        vol20 = sqrt(sumprod(w[-20:], w[-20:]) / 20) or 1e-4
        vol60 = sqrt(sumprod(w, w) / 60) or 1e-4
        r = lambda k: log(c[i] / c[i - k])
        sr = lambda k: log(sc[i] / sc[i - k])
        up = sum(x for x in w[-14:] if x > 0)
        down = -sum(x for x in w[-14:] if x < 0)
        sma50 = sum(c[i - 49:i + 1]) / 50
        sma200 = sum(c[i - 199:i + 1]) / 200
        hi20, lo20 = max(hi[i - 19:i + 1]), min(lo[i - 19:i + 1])
        lv = [log(max(x, 1.0)) for x in vol[i - 19:i + 1]]
        mlv = sum(lv) / 20
        slv = sqrt(sum((x - mlv) ** 2 for x in lv) / 19) or 1.0
        atr = sum((hi[j] - lo[j]) / c[j] for j in range(i - 13, i + 1)) / 14
        spy_sma200 = sum(x for x in sc[i - 199:i + 1] if x) / 200
        f = [
            r(1) / vol20, r(5) / (vol20 * sqrt(5)), r(20) / (vol20 * sqrt(20)),
            r(60) / (vol60 * sqrt(60)), r(120) / (vol60 * sqrt(120)), r(252) / (vol60 * sqrt(252)),
            (r(5) - sr(5)) / (vol20 * sqrt(5)), (r(20) - sr(20)) / (vol20 * sqrt(20)),
            log(vol20 / vol60), log(vol20 * sqrt(252)),
            up / (up + down) - 0.5 if up + down else 0.0,
            log(c[i] / sma50) / vol20, log(c[i] / sma200) / vol60,
            (c[i] - lo20) / (hi20 - lo20) - 0.5 if hi20 > lo20 else 0.0,
            c[i] / max(c[i - 251:i + 1]) - 1,
            (lv[-1] - mlv) / slv, atr / vol20,
            log(op[i] / c[i - 1]) / vol20 if op[i] > 0 else 0.0,
            log(vx[i]), log(vx[i] / vx[i - 5]), sr(20), log(sc[i] / spy_sma200) if spy_sma200 else 0.0,
        ]
        out.append([_clean(x) for x in f])
    return out


def vol_features(s: dict, vix: dict[int, float]) -> list[list[float] | None]:
    """HAR-style log-volatility predictors for every bar."""
    s = positive(s)
    days, c, hi, lo = s["day"], s["c"], s["h"], s["l"]
    vx = _ffill(days, vix)
    n = len(c)
    lr = [0.0] + [log(c[i] / c[i - 1]) if c[i] > 0 and c[i - 1] > 0 else 0.0 for i in range(1, n)]
    k4 = 4 * log(2)
    pk = [(log(hi[i] / lo[i]) ** 2) / k4 if hi[i] > 0 and lo[i] > 0 and hi[i] >= lo[i] else lr[i] ** 2 for i in range(n)]
    out: list[list[float] | None] = []
    ew = lr[1] ** 2 if n > 1 else 1e-4
    for i in range(n):
        ew = 0.94 * ew + 0.06 * lr[i] ** 2
        if i < 80 or not vx[i]:
            out.append(None)
            continue
        rv1 = sqrt(pk[i])
        rv5 = sqrt(sum(pk[i - 4:i + 1]) / 5)
        rv22 = sqrt(sum(pk[i - 21:i + 1]) / 22)
        w = lr[i - 59:i + 1]
        cv60 = sqrt(sumprod(w, w) / 60)
        out.append([log(max(rv1, 1e-4)), log(max(rv5, 1e-4)), log(max(rv22, 1e-4)), log(max(cv60, 1e-4)),
                    log(max(sqrt(ew), 1e-4)), log(vx[i] / 100 / sqrt(252))])
    return out


# ---------------------------------------------------------------- linear algebra

def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for k in range(n):
        p = max(range(k, n), key=lambda r: abs(m[r][k]))
        m[k], m[p] = m[p], m[k]
        piv = m[k][k] or 1e-12
        for r in range(k + 1, n):
            f = m[r][k] / piv
            if f:
                m[r] = [x - f * y for x, y in zip(m[r], m[k])]
    x = [0.0] * n
    for k in range(n - 1, -1, -1):
        x[k] = (m[k][n] - sumprod(m[k][k + 1:n], x[k + 1:])) / (m[k][k] or 1e-12)
    return x


def _standardize(rows: list[list[float]], means=None, sds=None):
    d = len(rows[0])
    if means is None:
        means = [sum(r[j] for r in rows) / len(rows) for j in range(d)]
        sds = [sqrt(sum((r[j] - means[j]) ** 2 for r in rows) / len(rows)) or 1.0 for j in range(d)]
    z = [[max(-4.0, min(4.0, (r[j] - means[j]) / sds[j])) for j in range(d)] for r in rows]
    return z, means, sds


def fit_logit(z: list[list[float]], y: list[int], lam: float = 3000.0, iters: int = 7) -> list[float]:
    """Ridge logistic regression by Newton/IRLS (column-wise sumprod keeps it fast)."""
    x = [[1.0] + r for r in z]
    d = len(x[0])
    cols = [[r[j] for r in x] for j in range(d)]
    w = [0.0] * d
    for _ in range(iters):
        p = [1 / (1 + exp(-max(-30.0, min(30.0, sumprod(w, r))))) for r in x]
        g = [pi - yi for pi, yi in zip(p, y)]
        q = [pi * (1 - pi) for pi in p]
        grad = [sumprod(g, cols[j]) + (lam * w[j] if j else 0.0) for j in range(d)]
        hess = [[0.0] * d for _ in range(d)]
        for j in range(d):
            qc = [a * b for a, b in zip(q, cols[j])]
            for k in range(j, d):
                hess[j][k] = hess[k][j] = sumprod(qc, cols[k])
            if j:
                hess[j][j] += lam
        step = _solve(hess, grad)
        w = [a - b for a, b in zip(w, step)]
    return w


def _logit_prob(w: list[float], z: list[float]) -> float:
    return 1 / (1 + exp(-max(-30.0, min(30.0, w[0] + sumprod(w[1:], z)))))


def fit_ols(x: list[list[float]], y: list[float], ridge: float = 1e-3) -> tuple[list[float], float]:
    xx = [[1.0] + r for r in x]
    d = len(xx[0])
    cols = [[r[j] for r in xx] for j in range(d)]
    a = [[sumprod(cols[i], cols[j]) + (ridge if i == j else 0.0) for j in range(d)] for i in range(d)]
    w = _solve(a, [sumprod(cols[i], y) for i in range(d)])
    resid = [yy - sumprod(w, r) for r, yy in zip(xx, y)]
    return w, sumprod(resid, resid) / max(1, len(resid))


def _quantile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


# ---------------------------------------------------------------- datasets & training

def _datasets(universe: dict[str, dict], spy: dict[int, float], vix: dict[int, float]):
    """Rows: (day, ticker, direction_features, vol_features, future_log_return, future_realised_vol)."""
    rows = []
    for ticker, s in universe.items():
        s = positive(s)
        c = s["c"]
        df, vf = direction_features(s, spy, vix), vol_features(s, vix)
        n = len(c)
        lr = [0.0] + [log(c[i] / c[i - 1]) if c[i] > 0 and c[i - 1] > 0 else 0.0 for i in range(1, n)]
        # The final bar may still be trading, so it is never used as a target.
        for i in range(n - 1 - HORIZON):
            if df[i] is None or vf[i] is None:
                continue
            fut = log(c[i + HORIZON] / c[i])
            frv = sqrt(sum(lr[j] ** 2 for j in range(i + 1, i + HORIZON + 1)) / HORIZON)
            rows.append((s["day"][i], ticker, df[i], vf[i], fut, frv))
    rows.sort(key=lambda r: (r[0], r[1]))
    return rows


def _fit(rows, lam: float) -> dict:
    """Fit direction + volatility models on the given rows."""
    z, means, sds = _standardize([r[2] for r in rows])
    y = [1 if r[4] > 0 else 0 for r in rows]
    w = fit_logit(z, y, lam)
    hw, s2 = fit_ols([r[3] for r in rows], [log(max(r[5], 1e-4)) for r in rows])
    state = {"logit": w, "means": means, "sds": sds, "har": hw, "har_s2": s2}
    scaled_by: dict[str, list[float]] = {}
    for r in rows:
        scaled_by.setdefault(r[1], []).append(r[4] / predicted_vol(state, r[3]))
    qs = lambda vals: [_quantile(vals, LOWER_Q), _quantile(vals, 0.5), _quantile(vals, UPPER_Q)]
    state["q"] = qs(sorted(v for vals in scaled_by.values() for v in vals))
    # Per-stock quantiles (fat tails differ between TSLA and BRK-B), shrunk toward the pooled ones.
    state["q_by"] = {}
    for t, vals in scaled_by.items():
        own, wt = qs(sorted(vals)), len(vals) / (len(vals) + 750)
        state["q_by"][t] = [wt * a + (1 - wt) * b for a, b in zip(own, state["q"])]
    pooled = sum(y) / len(y)
    by: dict[str, list[int]] = {}
    for r, yy in zip(rows, y):
        by.setdefault(r[1], []).append(yy)
    k = 250  # shrink each stock's up-rate toward the pooled rate
    state["base"] = {t: (sum(v) + k * pooled) / (len(v) + k) for t, v in by.items()}
    state["base_pooled"] = pooled
    return state


def predicted_vol(state: dict, vf: list[float]) -> float:
    """Predicted standard deviation of the HORIZON-day log return."""
    return exp(state["har"][0] + sumprod(state["har"][1:], vf) + state["har_s2"] / 2) * sqrt(HORIZON)


def _raw_prob(state: dict, df: list[float]) -> float:
    z, _, _ = _standardize([df], state["means"], state["sds"])
    return _logit_prob(state["logit"], z[0])


def train(universe: dict[str, dict], spy: dict[int, float], vix: dict[int, float], lam: float = 3000.0,
          test_days: int = 126, progress=None) -> dict:
    rows = _datasets(universe, spy, vix)
    if len(rows) < 2000:
        raise RuntimeError("Not enough history to train the model.")
    days = sorted({r[0] for r in rows})
    start = int(len(days) * 0.35)
    preds = []  # (ticker, raw_p, base, y, fut, lo, hi, old_lo, old_hi)
    folds = list(range(start, len(days), test_days))
    for fi, k in enumerate(folds):
        if progress:
            progress(f"Walk-forward test {fi + 1}/{len(folds)}")
        cutoff = days[max(0, k - HORIZON)]  # purge overlapping targets
        t0, t1 = days[k], days[min(len(days) - 1, k + test_days)] if k + test_days < len(days) else days[-1] + 1
        train_rows = [r for r in rows if r[0] < cutoff]
        test_rows = [r for r in rows if t0 <= r[0] < t1]
        if not test_rows or len(train_rows) < 1000:
            continue
        st = _fit(train_rows, lam)
        for r in test_rows:
            pv = predicted_vol(st, r[3])
            base = st["base"].get(r[1], st["base_pooled"])
            q = st["q_by"].get(r[1], st["q"])
            preds.append((r[1], _raw_prob(st, r[2]), base, 1 if r[4] > 0 else 0, r[4], q[0] * pv, q[2] * pv))
    if progress:
        progress("Training final model")
    state = _fit(rows, lam)
    state["metrics"] = _evaluate(preds)
    state["alpha"] = state["metrics"]["direction"]["alpha"]
    state["edge"] = state["metrics"]["direction"]["edge"]
    state["version"] = MODEL_VERSION
    state["trained_on"] = len(rows)
    state["first_day"], state["last_day"] = days[0], days[-1]
    return state


def _auc(ps: list[float], ys: list[int]) -> float:
    pairs = sorted(zip(ps, ys))
    pos = sum(ys)
    neg = len(ys) - pos
    if not pos or not neg:
        return 0.5
    rank_sum = sum(k for k, (_, y) in enumerate(pairs, 1) if y)
    return (rank_sum - pos * (pos + 1) / 2) / (pos * neg)


def _evaluate(preds) -> dict:
    n = len(preds)
    ys = [p[3] for p in preds]
    brier_base = sum((p[2] - p[3]) ** 2 for p in preds) / n
    best_alpha, best_brier = 0.0, brier_base
    for alpha in (0.25, 0.5, 0.75, 1.0):
        b = sum((p[2] + alpha * (p[1] - p[2]) - p[3]) ** 2 for p in preds) / n
        if b < best_brier - 1e-6:
            best_alpha, best_brier = alpha, b
    model_brier = sum((p[1] - p[3]) ** 2 for p in preds) / n
    acc_model = sum((p[1] > 0.5) == bool(p[3]) for p in preds) / n
    acc_base = max(sum(ys), n - sum(ys)) / n
    # Edge test: does the blended probability beat the base-rate forecast?
    # z-score of the per-sample Brier improvement (crude, ignores overlap → conservative threshold).
    diffs = [(p[2] - p[3]) ** 2 - (p[2] + best_alpha * (p[1] - p[2]) - p[3]) ** 2 for p in preds]
    md = sum(diffs) / n
    sd = sqrt(sum((d - md) ** 2 for d in diffs) / max(1, n - 1)) or 1e-9
    z = md / (sd / sqrt(n / HORIZON))  # effective sample size: overlapping 5-day windows
    edge = best_alpha > 0 and z > 2.0
    per_stock = {}
    for t in sorted({p[0] for p in preds}):
        sub = [p for p in preds if p[0] == t]
        cov = sum(p[5] <= p[4] <= p[6] for p in sub) / len(sub)
        per_stock[t] = {"n": len(sub), "range_coverage": round(cov, 3),
                        "up_rate": round(sum(p[3] for p in sub) / len(sub), 3),
                        "avg_width_pct": round(sum(exp(p[6]) - exp(p[5]) for p in sub) / len(sub) * 100, 2)}
    coverage = sum(p[5] <= p[4] <= p[6] for p in preds) / n
    return {
        "samples": n,
        "direction": {
            "auc": round(_auc([p[1] for p in preds], ys), 3),
            "accuracy_model": round(acc_model, 3),
            "accuracy_always_majority": round(acc_base, 3),
            "brier_skill_raw_model": round(1 - model_brier / brier_base, 4),
            "brier_skill_blended": round(1 - best_brier / brier_base, 4),
            "alpha": best_alpha, "edge": edge, "z": round(z, 2),
        },
        "range": {"target": round(UPPER_Q - LOWER_Q, 2), "coverage": round(coverage, 3),
                  "avg_width_pct": round(sum(exp(p[6]) - exp(p[5]) for p in preds) / n * 100, 2)},
        "per_stock": per_stock,
    }


def forecast(state: dict, ticker: str, s: dict, spy: dict[int, float], vix: dict[int, float], price: float) -> dict | None:
    """Forecast for the latest bar of one stock."""
    df = direction_features(s, spy, vix)[-1]
    vf = vol_features(s, vix)[-1]
    if df is None or vf is None or not price:
        return None
    base = state["base"].get(ticker, state["base_pooled"])
    raw = _raw_prob(state, df)
    p_up = base + state["alpha"] * (raw - base)
    pv = predicted_vol(state, vf)
    q_lo, q_mid, q_hi = state.get("q_by", {}).get(ticker, state["q"])
    annual = pv / sqrt(HORIZON) * sqrt(252)
    lean = p_up - 0.5
    if state["edge"] and abs(p_up - base) >= 0.04:
        signal = "Leaning higher" if p_up > base else "Leaning lower"
    else:
        signal = "No reliable edge"
    stock_metrics = state["metrics"]["per_stock"].get(ticker, {})
    dp = 5 if price < 20 else 2
    return {
        "horizon": HORIZON,
        "p_up": round(p_up, 3),
        "base_rate": round(base, 3),
        "raw_model_p_up": round(raw, 3),
        "signal": signal,
        "edge": bool(state["edge"] and signal != "No reliable edge"),
        "lean": round(lean, 3),
        "low": round(price * exp(q_lo * pv), dp),
        "mid": round(price * exp(q_mid * pv), dp),
        "high": round(price * exp(q_hi * pv), dp),
        "low_pct": round((exp(q_lo * pv) - 1) * 100, 2),
        "high_pct": round((exp(q_hi * pv) - 1) * 100, 2),
        "vol_5d_pct": round(pv * 100, 2),
        "vol_annual_pct": round(annual * 100, 1),
        "risk": "High" if annual > 0.45 else "Elevated" if annual > 0.30 else "Moderate" if annual > 0.18 else "Low",
        "range_coverage": stock_metrics.get("range_coverage"),
        "backtest_samples": stock_metrics.get("n", 0),
    }
