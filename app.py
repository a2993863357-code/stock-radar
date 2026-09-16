# -*- coding: utf-8 -*-
"""
app.py —— Stock Radar 本地股票行情分析服务（Flask 入口）。

功能：
  * 全市场快照 + 候选池 K 线抓取 → 本地缓存（data/），支持手动刷新
  * 首页「推荐购买指数 Top10」排行榜（0~100 综合评分 + 因子明细）
  * 涨幅榜 / 跌幅榜，支持近 7 日、近 1 月周期切换
  * 股票搜索（名称 / 代码 / 拼音首字母）→ 实时行情 + 区间涨跌幅 + 均线 + 评分
  * 启动时缓存过期自动后台刷新；内置每日定时刷新线程；
    另支持 `python app.py --refresh` 一次性刷新（供 Windows 计划任务调用）

启动：D:\\Python3.12.1\\python.exe app.py
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, jsonify, render_template, request

# 保证在 Windows 控制台（默认 GBK）下打印中文/emoji 不炸
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

import config
import data_source as ds
import scoring

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["JSON_AS_ASCII"] = False

# ---------------------------------------------------------------------------
# 全局状态
# ---------------------------------------------------------------------------
_lock = threading.Lock()
REFRESH_STATE = {
    "running": False,
    "stage": "idle",
    "percent": 0,
    "message": "尚未刷新",
    "started_at": None,
    "finished_at": None,
    "error": None,
    "last_summary": None,
}

_mem = {"scores": None, "scores_key": None, "snapshot": None, "snapshot_key": None}


def _log(msg: str) -> None:
    line = "[%s] %s" % (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(config.REFRESH_LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def _set_state(**kw) -> None:
    with _lock:
        REFRESH_STATE.update(kw)


def get_scores_payload() -> dict:
    payload = ds.load_scores_payload()
    key = payload.get("updated_at")
    if _mem["scores"] is None or _mem["scores_key"] != key:
        _mem["scores"] = payload
        _mem["scores_key"] = key
    return _mem["scores"]


def get_snapshot() -> list:
    payload = ds._read_json(config.SNAPSHOT_FILE) or {}
    key = payload.get("updated_at")
    if _mem["snapshot"] is None or _mem["snapshot_key"] != key:
        _mem["snapshot"] = payload.get("stocks") or []
        _mem["snapshot_key"] = key
    return _mem["snapshot"]


def get_records() -> list:
    return get_scores_payload().get("items") or []


# ---------------------------------------------------------------------------
# 刷新流水线
# ---------------------------------------------------------------------------
def _apply_ths_snapshot(item: dict, rows: list) -> None:
    """用同花顺日K线末行覆盖快照里的量价字段，保证展示口径与评分口径一致。

    覆盖：price / pct_chg / open / high / low / amount / turnover
    保留：pe / pb / total_mv / circ_mv / volume_ratio / industry / chg_5d / chg_60d
          （同花顺日K接口不提供这些静态或衍生字段，继续由全市场快照提供）
    """
    if config.KLINE_SOURCE != "ths" or not rows:
        return
    last = rows[-1]
    if last.get("close") is not None:
        item["price"] = last["close"]
    if last.get("pct") is not None:
        item["pct_chg"] = last["pct"]
    for k in ("open", "high", "low"):
        if last.get(k) is not None:
            item[k] = last[k]
    if last.get("amount") is not None:
        item["amount"] = last["amount"]
    if last.get("turnover") is not None:
        item["turnover"] = last["turnover"]
    item["quote_source"] = "ths_kline"


def run_refresh(force_kline: bool = False, reason: str = "manual") -> dict:
    """完整刷新：全市场快照 → 候选池 → 评分 → 落盘。"""
    t0 = time.time()
    _set_state(
        running=True, stage="snapshot", percent=2,
        message="正在拉取全市场快照…", started_at=dt.datetime.now().isoformat(timespec="seconds"),
        finished_at=None, error=None,
    )
    _log("刷新开始（原因：%s）" % reason)

    def snap_progress(done, total):
        pct = 2 + int(done / max(total, 1) * 28)
        _set_state(percent=pct, message="全市场快照 %d/%d 页" % (done, total))

    stocks = ds.fetch_snapshot(on_progress=snap_progress)
    ds.save_snapshot(stocks)
    _log("快照完成：%d 只股票" % len(stocks))

    # 候选池：按成交额降序（无成交额的排在后面），剔除停牌（无最新价）
    tradable = [s for s in stocks if s.get("price") is not None]
    tradable.sort(key=lambda s: s.get("amount") or 0, reverse=True)
    pool = tradable[: config.POOL_SIZE]
    _set_state(
        stage="kline", percent=32,
        message="正在抓取候选池 %d 只股票日K线…" % len(pool),
    )

    def kl_progress(done, total):
        pct = 32 + int(done / max(total, 1) * 58)
        _set_state(percent=pct, message="日K线 %d/%d" % (done, total))

    klines = ds.fetch_klines_bulk(
        [s["secid"] for s in pool], refresh=force_kline, on_progress=kl_progress
    )

    _set_state(stage="scoring", percent=92, message="正在计算综合评分…")
    records = []
    for s in pool:
        rows = klines.get(s["secid"]) or []
        if not rows:
            continue
        try:
            _apply_ths_snapshot(s, rows)
            m = scoring.compute_metrics(rows, s)
            records.append(scoring.build_item(s, m))
        except Exception:  # noqa: BLE001
            _log("评分失败 %s: %s" % (s.get("code"), traceback.format_exc(limit=2)))
    records.sort(key=lambda r: r["score"], reverse=True)

    # Top 榜叠加同花顺实时行情（realhead），让首页显示的是同花顺口径的最新价
    top_for_quote = records[: max(config.TOP_N * 2, 20)]
    if config.QUOTE_SOURCE == "ths" and top_for_quote:
        _set_state(stage="quote", percent=96, message="正在获取同花顺实时行情…")
        try:
            import data_source_ths as ths
            qs = ths.fetch_quotes_bulk([r["code"] for r in top_for_quote])
            hit = 0
            for r in top_for_quote:
                q = qs.get(r["code"]) or {}
                if q.get("price") is None:
                    continue
                r["rt"] = {
                    "price": q.get("price"), "pct_chg": q.get("pct_chg"),
                    "open": q.get("open"), "high": q.get("high"), "low": q.get("low"),
                    "prev_close": q.get("prev_close"),
                    "volume": q.get("volume"), "amount": q.get("amount"),
                    "pe": q.get("pe"), "source": "ths", "quote_time": q.get("quote_time"),
                }
                # 有实时行情时以实时价为准展示
                r["price"] = q.get("price")
                if q.get("pct_chg") is not None:
                    r["pct_chg"] = q.get("pct_chg")
                r["quote_source"] = "ths_realtime"
                hit += 1
            _log("同花顺实时行情命中 %d/%d" % (hit, len(top_for_quote)))
        except Exception as exc:  # noqa: BLE001
            _log("同花顺实时行情获取失败（不影响评分结果）：%s" % exc)

    summary = scoring.summary_stats(records)
    meta = {
        "pool_size": len(pool),
        "universe_size": len(stocks),
        "summary": summary,
        "refresh_reason": reason,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    ds.save_scores(records, meta)
    ds.save_meta({
        "last_refresh": dt.datetime.now().isoformat(timespec="seconds"),
        "last_auto_date": dt.date.today().strftime("%Y-%m-%d") if reason == "scheduler" else ds.load_meta().get("last_auto_date"),
        "last_reason": reason,
    })

    elapsed = round(time.time() - t0, 1)
    _set_state(
        running=False, stage="done", percent=100,
        message="刷新完成：候选池 %d 只，耗时 %.1fs" % (len(records), elapsed),
        finished_at=dt.datetime.now().isoformat(timespec="seconds"),
        last_summary={**summary, "elapsed_sec": elapsed},
    )
    _log("刷新完成：universe=%d pool=%d scored=%d 耗时=%.1fs" % (len(stocks), len(pool), len(records), elapsed))
    return {"ok": True, "pool_size": len(pool), "scored": len(records), "elapsed_sec": elapsed}


def start_refresh_async(force_kline: bool = False, reason: str = "manual") -> bool:
    with _lock:
        if REFRESH_STATE["running"]:
            return False
    threading.Thread(
        target=_refresh_guard, kwargs={"force_kline": force_kline, "reason": reason}, daemon=True
    ).start()
    return True


def _refresh_guard(force_kline: bool, reason: str):
    try:
        run_refresh(force_kline=force_kline, reason=reason)
    except Exception as exc:  # noqa: BLE001
        _log("刷新失败：%s\n%s" % (exc, traceback.format_exc(limit=4)))
        _set_state(
            running=False, stage="error", message="刷新失败：%s" % exc,
            finished_at=dt.datetime.now().isoformat(timespec="seconds"), error=str(exc),
        )


# ---------------------------------------------------------------------------
# 按需评分（用于搜索命中但不在候选池内的股票）
# ---------------------------------------------------------------------------
def lazy_score(item: dict, with_bars: bool = False) -> dict:
    secid = item.get("secid") or ""
    if not secid:
        return {"error": "无法解析该股票的 secid"}
    try:
        rows = ds.fetch_kline(secid)
    except Exception as exc:  # noqa: BLE001
        return {"error": "K线获取失败：%s" % exc}
    if not rows:
        return {"error": "未获取到该股票的日K线数据"}
    m = scoring.compute_metrics(rows, item)
    obj = scoring.build_item(item, m)
    if with_bars:
        obj["bars"] = [
            {"date": r["date"], "close": r["close"], "pct": r.get("pct")} for r in rows[-60:]
        ]
    return obj


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    return jsonify({
        "ok": True,
        "cache": ds.cache_status(),
        "refresh": REFRESH_STATE,
        "config": {
            "pool_size": config.POOL_SIZE,
            "top_n": config.TOP_N,
            "auto_refresh_time": config.AUTO_REFRESH_TIME,
            "cache_max_age_hours": config.CACHE_MAX_AGE_HOURS,
            "scheduler_enabled": config.ENABLE_SCHEDULER,
        },
        "sources": _sources_payload(),
        "server_time": dt.datetime.now().isoformat(timespec="seconds"),
    })


# ---------------------------------------------------------------------------
# 数据源管理（同花顺 / 东方财富）
# ---------------------------------------------------------------------------
_SOURCE_LABELS = {
    "ths": "同花顺 d.10jqka.com.cn",
    "eastmoney": "东方财富 push2 / push2his",
}


def _sources_payload() -> dict:
    """当前数据源分工 + 可切换项。"""
    return {
        "active": {
            "snapshot": config.SNAPSHOT_SOURCE,
            "kline": config.KLINE_SOURCE,
            "quote": config.QUOTE_SOURCE,
        },
        "labels": {
            "snapshot": _SOURCE_LABELS.get(config.SNAPSHOT_SOURCE, config.SNAPSHOT_SOURCE),
            "kline": _SOURCE_LABELS.get(config.KLINE_SOURCE, config.KLINE_SOURCE),
            "quote": _SOURCE_LABELS.get(config.QUOTE_SOURCE, config.QUOTE_SOURCE),
        },
        "switchable": {
            "kline": ["ths", "eastmoney"],
            "quote": ["ths", "eastmoney"],
        },
        "note": ("全市场列表固定由东财提供：同花顺排行页有反爬，仅第 1 页可取，"
                 "第 2 页起返回 HTTP 401/403（需 hexin-v token），无法覆盖 5900+ 只股票。"),
    }


@app.route("/api/source")
def api_source():
    return jsonify({"ok": True, **_sources_payload(), "fallbacks": ds.fallback_events()[-20:]})


@app.route("/api/source", methods=["POST"])
def api_source_switch():
    """运行时切换 K 线 / 实时行情数据源（不落盘，重启后回到环境变量默认值）。"""
    body = request.get_json(silent=True) or {}
    body.update(request.args.to_dict())
    changed = []
    for field, attr in (("kline", "KLINE_SOURCE"), ("quote", "QUOTE_SOURCE")):
        val = (body.get(field) or "").strip().lower()
        if not val:
            continue
        if val not in ("ths", "eastmoney"):
            return jsonify({"ok": False, "message": "不支持的 %s 数据源：%s" % (field, val)}), 400
        setattr(config, attr, val)
        changed.append({"field": field, "value": val})
    if not changed:
        return jsonify({"ok": False, "message": "请指定 kline 或 quote 参数"}), 400
    _log("数据源已切换：%s" % changed)
    return jsonify({"ok": True, "changed": changed, **_sources_payload()})


@app.route("/api/source/health")
def api_source_health():
    """同花顺各接口连通性自检（K线 / 实时行情 / 排行页）。"""
    code = (request.args.get("code") or "600519").strip()
    try:
        import data_source_ths as ths
        return jsonify({"ok": True, "code": code, "ths": ths.health_check(code)})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "message": "同花顺自检失败：%s" % exc}), 502


@app.route("/api/quote/<code>")
def api_quote(code):
    """个股实时行情（默认同花顺 realhead，失败自动回退东财）。"""
    code = code.strip()
    secid = _to_secid(code)
    if not secid:
        return jsonify({"ok": False, "message": "无法解析代码 %s 的市场归属" % code}), 400
    q = ds.fetch_quote(secid)
    if not q:
        return jsonify({"ok": False, "message": "未获取到 %s 的实时行情" % code}), 502
    return jsonify({"ok": True, "quote": q,
                    "source_label": _SOURCE_LABELS.get(q.get("source"), q.get("source"))})


def _to_secid(code: str) -> str:
    """纯代码 → 东财 secid（1=沪市, 0=深市/北交所）。未知代码回空。"""
    code = code.strip()
    if "." in code:
        return code
    if not (code.isdigit() and len(code) == 6):
        return ""
    if code[0] == "6" or code.startswith("688") or code.startswith("9"):
        return "1.%s" % code
    if code[0] in ("0", "3", "2", "4", "8"):
        return "0.%s" % code
    return ""


@app.route("/api/config")
def api_config():
    return jsonify({
        "ok": True,
        "weights": config.SCORE_WEIGHTS,
        "factor_labels": scoring.FACTOR_LABELS,
        "factor_order": scoring.FACTOR_ORDER,
        "pool_size": config.POOL_SIZE,
        "top_n": config.TOP_N,
        "trading_days_1w": config.TRADING_DAYS_1W,
        "trading_days_1m": config.TRADING_DAYS_1M,
    })


@app.route("/api/refresh", methods=["POST", "GET"])
def api_refresh():
    force = request.args.get("force", "0") in ("1", "true", "yes")
    started = start_refresh_async(force_kline=force, reason="manual")
    if not started:
        return jsonify({"ok": False, "message": "已有刷新任务在进行中", "refresh": REFRESH_STATE}), 409
    return jsonify({"ok": True, "message": "刷新任务已启动", "refresh": REFRESH_STATE})


@app.route("/api/recommend")
def api_recommend():
    limit = min(int(request.args.get("limit", config.TOP_N)), 100)
    weights = _weights_from_args()
    records = get_records()
    if weights:
        records = _rescore(records, weights)
    else:
        records = sorted(records, key=lambda r: r["score"], reverse=True)
    recs = [r for r in records if r.get("data_ok")] or records
    payload = get_scores_payload()
    return jsonify({
        "ok": True,
        "updated_at": payload.get("updated_at"),
        "pool_size": payload.get("pool_size", 0),
        "universe_size": payload.get("universe_size", 0),
        "summary": payload.get("summary"),
        "weights": weights or config.SCORE_WEIGHTS,
        "count": len(recs[:limit]),
        "items": recs[:limit],
    })


@app.route("/api/rank")
def api_rank():
    period = request.args.get("period", "7")
    order = request.args.get("order", "desc")
    limit = min(int(request.args.get("limit", config.RANK_LIMIT)), 500)
    weights = _weights_from_args()
    records = get_records()
    if weights:
        records = _rescore(records, weights)
        records.sort(key=lambda r: r["score"], reverse=True)
    rows = scoring.rank_list(records, period=period, order=order, limit=limit)
    payload = get_scores_payload()
    return jsonify({
        "ok": True,
        "period": period,
        "period_label": "近7日" if str(period) == "7" else "近1月",
        "order": order,
        "updated_at": payload.get("updated_at"),
        "pool_size": payload.get("pool_size", 0),
        "count": len(rows),
        "items": rows,
    })


@app.route("/api/search")
def api_search():
    q = (request.args.get("q") or "").strip()
    limit = min(int(request.args.get("limit", 8)), 20)
    if not q:
        return jsonify({"ok": False, "message": "请输入股票名称或代码"}), 400

    snapshot = get_snapshot()
    matches = ds.search_stocks(q, snapshot, limit=limit)
    if not matches:
        return jsonify({"ok": True, "query": q, "count": 0, "items": [],
                        "message": "未找到匹配的股票（可尝试输入完整代码或名称）"})

    scored_index = {r["code"]: r for r in get_records()}
    out = []
    for i, it in enumerate(matches):
        code = it.get("code")
        if code in scored_index and i < 3:
            out.append(scored_index[code])
        elif i < 3:
            out.append(lazy_score(it))
        else:
            out.append({
                "code": code, "name": it.get("name"), "secid": it.get("secid"),
                "industry": it.get("industry"), "price": it.get("price"),
                "pct_chg": it.get("pct_chg"), "amount": it.get("amount"),
                "turnover": it.get("turnover"), "volume_ratio": it.get("volume_ratio"),
                "pe": it.get("pe"), "pb": it.get("pb"), "chg_5d": it.get("chg_5d"),
                "score": None, "brief": True,
            })
    return jsonify({"ok": True, "query": q, "count": len(out), "items": out})


@app.route("/api/stock/<code>")
def api_stock(code):
    code = code.strip()
    item = None
    for s in get_snapshot():
        if s.get("code") == code:
            item = s
            break
    if item is None:
        matches = ds.search_stocks(code, get_snapshot(), limit=1)
        if not matches:
            return jsonify({"ok": False, "message": "未找到代码为 %s 的股票" % code}), 404
        item = matches[0]
    obj = lazy_score(item, with_bars=True)
    if obj.get("error"):
        return jsonify({"ok": False, "message": obj["error"]}), 502
    # 叠加个股实时行情（默认同花顺 realhead），失败不影响主结果
    try:
        obj["quote"] = ds.fetch_quote(item.get("secid"))
    except Exception as exc:  # noqa: BLE001
        obj["quote"] = {"error": str(exc)}
    obj["kline_source"] = config.KLINE_SOURCE
    obj["snapshot_source"] = config.SNAPSHOT_SOURCE
    return jsonify({"ok": True, "item": obj})


def _weights_from_args():
    """支持 ?w_momentum_7d=0.2&... 动态覆盖因子权重。"""
    keys = list(config.SCORE_WEIGHTS.keys())
    got = {}
    for k in keys:
        v = request.args.get("w_" + k)
        if v not in (None, ""):
            try:
                got[k] = float(v)
            except ValueError:
                pass
    return got or None


def _rescore(records, weights):
    out = []
    w = dict(config.SCORE_WEIGHTS)
    w.update(weights)
    total_w = sum(w.values()) or 1.0
    w = {k: v / total_w for k, v in w.items()}
    for r in records:
        f = r.get("factors") or {}
        new = sum(f.get(k, 50.0) * w.get(k, 0.0) for k in scoring.FACTOR_ORDER)
        if not r.get("data_ok"):
            new = new * 0.6 + 50.0 * 0.4
        nr = dict(r)
        nr["score"] = round(max(0.0, min(100.0, new)), 2)
        nr["weights"] = {k: round(v, 4) for k, v in w.items()}
        out.append(nr)
    return out


# ---------------------------------------------------------------------------
# 后台预热 / 定时调度
# ---------------------------------------------------------------------------
def _bootstrap_refresh():
    """启动时若缓存过期或为空，则后台刷新一次。"""
    if ds.cache_is_fresh() and get_records():
        _log("缓存有效（age=%.2fh），跳过启动刷新" % ds.cache_age_hours())
        return
    _log("缓存缺失或已过期，开始启动刷新…")
    start_refresh_async(force_kline=False, reason="startup")


def _scheduler_loop():
    """每个交易日 AUTO_REFRESH_TIME 定时刷新一次。"""
    _log("内置调度线程已启动，每日 %s 自动刷新" % config.AUTO_REFRESH_TIME)
    hh, mm = [int(x) for x in config.AUTO_REFRESH_TIME.split(":")]
    while True:
        try:
            now = dt.datetime.now()
            meta = ds.load_meta()
            today = now.strftime("%Y-%m-%d")
            due = now.hour > hh or (now.hour == hh and now.minute >= mm)
            weekday = now.weekday() < 5
            if due and weekday and meta.get("last_auto_date") != today:
                if start_refresh_async(force_kline=False, reason="scheduler"):
                    _log("触发每日定时刷新")
                else:
                    _log("定时刷新跳过：已有任务在运行")
            time.sleep(60)
        except Exception:  # noqa: BLE001
            _log("调度线程异常：%s" % traceback.format_exc(limit=2))
            time.sleep(120)


def main():
    parser = argparse.ArgumentParser(description="Stock Radar 股票行情分析服务")
    parser.add_argument("--refresh", action="store_true", help="执行一次全量刷新后退出（供计划任务调用）")
    parser.add_argument("--force-kline", action="store_true", help="刷新时强制重拉 K 线（忽略当日缓存）")
    parser.add_argument("--host", default=config.HOST)
    parser.add_argument("--port", type=int, default=config.PORT)
    parser.add_argument("--no-scheduler", action="store_true", help="不启动内置定时调度线程")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = parser.parse_args()

    if args.refresh:
        try:
            res = run_refresh(force_kline=args.force_kline, reason="cli")
            print("刷新结果：", res)
            return 0
        except Exception as exc:  # noqa: BLE001
            print("刷新失败：", exc)
            return 1

    _bootstrap_refresh()
    if config.ENABLE_SCHEDULER and not args.no_scheduler:
        threading.Thread(target=_scheduler_loop, daemon=True).start()

    url = "http://%s:%d/" % (args.host, args.port)
    print("=" * 62)
    print(" Stock Radar 已启动 -> %s" % url)
    print(" 数据目录：%s" % config.DATA_DIR)
    print("=" * 62, flush=True)

    if not args.no_browser:
        def _open():
            time.sleep(1.2)
            try:
                import webbrowser
                webbrowser.open(url)
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=_open, daemon=True).start()

    app.run(host=args.host, port=args.port, debug=False, use_reloader=False, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
