# -*- coding: utf-8 -*-
"""
scoring.py —— 0~100 综合评分模型（推荐购买指数）。

因子（权重可在 config.SCORE_WEIGHTS 中配置，也可由接口动态覆盖）：
  1. momentum_7d  近 7 个交易日涨跌幅
  2. momentum_1m  近 1 个月（21 个交易日）涨跌幅
  3. activity     量比 / 换手率活跃度
  4. trend        MA5/MA10/MA20 均线多头排列与价格位置
  5. risk         区间年化波动率与最大回撤（越低越好）
  6. valuation    PE / PB 估值合理性

每个因子先映射到 0~100，再按权重加权求和得到综合评分。
所有映射均为分段线性（_ramp），避免极端值主导，保证评分可解释。
"""
from __future__ import annotations

import math
import datetime as dt

import config

FACTOR_LABELS = {
    "momentum_7d": "近7日涨跌幅",
    "momentum_1m": "近1月涨跌幅",
    "activity": "量比/换手活跃度",
    "trend": "均线多头排列",
    "risk": "波动率/回撤",
    "valuation": "PE/PB估值",
}

FACTOR_ORDER = ["momentum_7d", "momentum_1m", "activity", "trend", "risk", "valuation"]


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _ramp(x, points, default=50.0):
    """分段线性映射。points 为 [(x1, y1), (x2, y2), ...]，按 x 升序。"""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return default
    pts = points
    if x <= pts[0][0]:
        return float(pts[0][1])
    if x >= pts[-1][0]:
        return float(pts[-1][1])
    for i in range(len(pts) - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        if x0 <= x <= x1:
            if x1 == x0:
                return float(y1)
            k = (x - x0) / (x1 - x0)
            return float(y0 + k * (y1 - y0))
    return default


def _clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def _sma(values, n):
    if len(values) < n or n <= 0:
        return None
    return sum(values[-n:]) / float(n)


# ---------------------------------------------------------------------------
# 指标计算
# ---------------------------------------------------------------------------
def compute_metrics(rows, item=None):
    """由日 K 线计算区间涨跌幅、均线、波动率、最大回撤等指标。

    rows: [{date, open, close, high, low, volume, amount, pct}, ...] 升序
    item: 可选，全市场快照中的该股票记录（用于补充量比/换手/估值）
    """
    item = item or {}
    closes = [r["close"] for r in rows if r.get("close")]
    dates = [r["date"] for r in rows if r.get("close")]
    highs = [r.get("high") for r in rows]
    lows = [r.get("low") for r in rows]
    n = len(closes)

    m = {
        "bars": n,
        "last_date": dates[-1] if dates else None,
        "last_close": closes[-1] if n else None,
        "ma5": _sma(closes, 5),
        "ma10": _sma(closes, 10),
        "ma20": _sma(closes, 20),
        "ma60": _sma(closes, 60),
        "ret7": None,
        "ret21": None,
        "ret5": None,
        "ret60": None,
        "volatility": None,      # 近 60 交易日年化波动率 %
        "max_drawdown": None,    # 近 60 交易日最大回撤 %（负数）
        "avg_amount_5d": None,   # 近 5 日均成交额（元）
        "data_ok": False,
    }

    def _ret(k):
        if n > k and closes[-1 - k]:
            return (closes[-1] / closes[-1 - k] - 1.0) * 100.0
        return None

    m["ret5"] = _ret(5)
    m["ret7"] = _ret(config.TRADING_DAYS_1W)
    m["ret21"] = _ret(config.TRADING_DAYS_1M)
    m["ret60"] = _ret(60)

    # 波动率 / 回撤：取最近 60 个交易日
    win = closes[-61:] if n >= 61 else closes
    if len(win) >= 10:
        rets = [win[i] / win[i - 1] - 1.0 for i in range(1, len(win)) if win[i - 1]]
        if len(rets) >= 5:
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
            m["volatility"] = math.sqrt(var) * math.sqrt(252) * 100.0
        peak, mdd = win[0], 0.0
        for px in win:
            peak = max(peak, px)
            if peak:
                mdd = min(mdd, (px / peak - 1.0) * 100.0)
        m["max_drawdown"] = mdd

    amounts = [r.get("amount") for r in rows[-5:] if r.get("amount")]
    if amounts:
        m["avg_amount_5d"] = sum(amounts) / len(amounts)

    # 数据充分性：至少 22 根 K 线才能算 1 月区间
    m["data_ok"] = n > config.TRADING_DAYS_1M and m["ma20"] is not None

    # 均线多头排列判定
    ma5, ma10, ma20 = m["ma5"], m["ma10"], m["ma20"]
    price = m["last_close"]
    m["ma_bull"] = bool(ma5 and ma10 and ma20 and ma5 > ma10 > ma20)
    m["above_ma5"] = bool(ma5 and price and price > ma5)
    m["above_ma20"] = bool(ma20 and price and price > ma20)
    # MA20 斜率：过去 5 日 MA20 的变化
    if n >= 25:
        ma20_prev = _sma(closes[:-5], 20)
        m["ma20_slope"] = (ma20 / ma20_prev - 1.0) * 100.0 if ma20_prev else None
    else:
        m["ma20_slope"] = None

    m["name"] = item.get("name")
    return m


# ---------------------------------------------------------------------------
# 因子打分
# ---------------------------------------------------------------------------
def factor_scores(item, m):
    """返回 {factor: 0~100} 及说明信息。"""
    # --- 1/2. 动量：区间涨跌幅 -------------------------------------------
    def _mom(ret):
        # -25% -> 0 分；0 -> 50 分；+15% -> 80 分；+40% -> 95 分；+80% -> 100 分
        return _ramp(ret, [(-25, 0), (-12, 20), (0, 50), (8, 70), (15, 80), (40, 95), (80, 100)])

    s7 = _mom(m.get("ret7"))
    s21 = _mom(m.get("ret21"))
    # 7 日与 5 日都缺失时用快照 f109（5日涨跌幅）兜底
    if m.get("ret7") is None and item.get("chg_5d") is not None:
        s7 = _ramp(item["chg_5d"], [(-20, 0), (-8, 25), (0, 50), (8, 70), (20, 90), (50, 100)])

    # --- 3. 活跃度：量比 + 换手率 ----------------------------------------
    vr = item.get("volume_ratio")
    to = item.get("turnover")
    vr_score = _ramp(vr, [(0, 10), (0.6, 35), (1.0, 50), (1.5, 68), (2.5, 85), (5, 95), (12, 100)])
    to_score = _ramp(to, [(0, 10), (0.8, 35), (2, 60), (4, 78), (8, 90), (15, 90), (30, 60)])
    activity = 0.5 * vr_score + 0.5 * to_score

    # --- 4. 均线排列 ------------------------------------------------------
    trend = 50.0
    trend += 12.0 if m.get("ma_bull") else 0.0
    trend += 10.0 if m.get("above_ma5") else -6.0
    trend += 9.0 if m.get("above_ma20") else -8.0
    slope = m.get("ma20_slope")
    if slope is not None:
        trend += _ramp(slope, [(-4, -12), (0, 0), (2, 6), (6, 12), (15, 16)])
    trend = _clamp(trend)

    # --- 5. 风险：波动率 + 最大回撤 ---------------------------------------
    vol_score = _ramp(m.get("volatility"), [(12, 95), (25, 80), (40, 55), (60, 30), (90, 10), (150, 0)])
    dd = m.get("max_drawdown")
    dd_score = _ramp(dd, [(-60, 0), (-40, 15), (-25, 35), (-15, 60), (-8, 80), (0, 95)])
    risk = 0.5 * vol_score + 0.5 * dd_score

    # --- 6. 估值 ----------------------------------------------------------
    pe = item.get("pe")
    pb = item.get("pb")
    if pe is None:
        pe_score = 50.0
    elif pe <= 0:
        pe_score = 25.0                       # 亏损
    else:
        pe_score = _ramp(pe, [(5, 95), (15, 88), (30, 72), (60, 50), (100, 32), (200, 15), (500, 5)])
    if pb is None:
        pb_score = 50.0
    elif pb <= 0:
        pb_score = 25.0
    else:
        pb_score = _ramp(pb, [(0.6, 95), (1.5, 85), (3, 70), (6, 48), (10, 30), (20, 12), (50, 3)])
    valuation = 0.6 * pe_score + 0.4 * pb_score

    return {
        "momentum_7d": round(_clamp(s7), 2),
        "momentum_1m": round(_clamp(s21), 2),
        "activity": round(_clamp(activity), 2),
        "trend": round(_clamp(trend), 2),
        "risk": round(_clamp(risk), 2),
        "valuation": round(_clamp(valuation), 2),
    }


def score_stock(item, m, weights=None):
    """综合评分，返回完整评分对象。"""
    w = dict(config.SCORE_WEIGHTS)
    if weights:
        for k, v in weights.items():
            if k in w and v is not None:
                w[k] = float(v)
    total_w = sum(w.values()) or 1.0
    w = {k: v / total_w for k, v in w.items()}

    fs = factor_scores(item, m)
    total = sum(fs[k] * w.get(k, 0.0) for k in FACTOR_ORDER)

    # 数据不足时向中性回归，避免误荐
    if not m.get("data_ok"):
        total = total * 0.6 + 50.0 * 0.4

    return {
        "score": round(_clamp(total), 2),
        "factors": fs,
        "weights": {k: round(v, 4) for k, v in w.items()},
        "data_ok": bool(m.get("data_ok")),
    }


# ---------------------------------------------------------------------------
# 汇总构建
# ---------------------------------------------------------------------------
def build_item(item, m, weights=None):
    """把快照记录 + 指标 + 评分合成一条可用于前端展示的完整记录。"""
    sc = score_stock(item, m, weights)
    return {
        "code": item.get("code"),
        "name": item.get("name"),
        "secid": item.get("secid"),
        "industry": item.get("industry"),
        "price": item.get("price") if item.get("price") is not None else m.get("last_close"),
        "pct_chg": item.get("pct_chg"),
        "open": item.get("open"),
        "high": item.get("high"),
        "low": item.get("low"),
        "quote_source": item.get("quote_source"),
        "amount": item.get("amount"),
        "turnover": item.get("turnover"),
        "volume_ratio": item.get("volume_ratio"),
        "pe": item.get("pe"),
        "pb": item.get("pb"),
        "total_mv": item.get("total_mv"),
        "circ_mv": item.get("circ_mv"),
        "chg_5d": item.get("chg_5d"),
        "chg_60d": item.get("chg_60d"),
        "ret7": _r(m.get("ret7")),
        "ret21": _r(m.get("ret21")),
        "ret5": _r(m.get("ret5")),
        "ma5": _r(m.get("ma5"), 3),
        "ma10": _r(m.get("ma10"), 3),
        "ma20": _r(m.get("ma20"), 3),
        "ma_bull": bool(m.get("ma_bull")),
        "volatility": _r(m.get("volatility")),
        "max_drawdown": _r(m.get("max_drawdown")),
        "bars": m.get("bars"),
        "last_date": m.get("last_date"),
        "score": sc["score"],
        "factors": sc["factors"],
        "weights": sc["weights"],
        "data_ok": sc["data_ok"],
        "reasons": build_reasons(item, m, sc),
    }


def _r(v, nd=2):
    if v is None:
        return None
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None


def build_reasons(item, m, sc):
    """生成中文推荐理由 / 风险提示（基于真实因子值，不做无依据推断）。"""
    f = sc["factors"]
    pros, cons = [], []
    if m.get("ret7") is not None and m["ret7"] > 2:
        pros.append("近7日上涨 %.2f%%" % m["ret7"])
    if m.get("ret21") is not None and m["ret21"] > 5:
        pros.append("近1月上涨 %.2f%%" % m["ret21"])
    if m.get("ma_bull"):
        pros.append("MA5>MA10>MA20 多头排列")
    if (item.get("volume_ratio") or 0) >= 1.5:
        pros.append("量比 %.2f 放量活跃" % item["volume_ratio"])
    if f.get("valuation", 0) >= 70:
        pros.append("PE/PB 估值处于合理区间")
    if f.get("risk", 0) >= 70:
        pros.append("区间波动与回撤可控")

    if m.get("ret7") is not None and m["ret7"] < -3:
        cons.append("近7日下跌 %.2f%%" % m["ret7"])
    if m.get("volatility") is not None and m["volatility"] >= 50:
        cons.append("年化波动率 %.1f%% 偏高" % m["volatility"])
    if m.get("max_drawdown") is not None and m["max_drawdown"] <= -20:
        cons.append("区间最大回撤 %.1f%%" % m["max_drawdown"])
    if (item.get("pe") or 0) > 80:
        cons.append("市盈率 %.1f 偏高" % item["pe"])
    if (item.get("pe") or 0) < 0:
        cons.append("当前处于亏损状态（PE 为负）")
    if not m.get("data_ok"):
        cons.append("K线数据不足，评分已做中性化处理")
    return {"pros": pros[:4], "cons": cons[:3]}


def rank_list(records, period="7", order="desc", limit=50):
    """按区间涨跌幅排序（period: '7' 近7日 / '21' 近1月）。"""
    key = "ret7" if str(period) == "7" else "ret21"
    rows = [r for r in records if r.get(key) is not None]
    rows.sort(key=lambda r: r[key], reverse=(order != "asc"))
    return rows[:limit]


def summary_stats(records):
    """候选池概览统计，用于首页展示。"""
    ups = [r for r in records if (r.get("ret7") or 0) > 0]
    scores = [r["score"] for r in records if r.get("score") is not None]
    return {
        "pool_count": len(records),
        "up_7d": len(ups),
        "down_7d": len(records) - len(ups),
        "avg_score": round(sum(scores) / len(scores), 2) if scores else None,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
