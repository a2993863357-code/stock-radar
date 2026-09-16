# -*- coding: utf-8 -*-
"""
data_source_ths.py —— 同花顺（10jqka）公开行情接口封装。

对接的同花顺免 token 接口（2026-09 实测可用）：
  1. 日 K 线   http://d.10jqka.com.cn/v6/line/hs_{code}/01/{year}.js
       JSONP：quotebridge_v6_line_hs_600519_01_2026({"data":"日期,开,高,低,收,量,额,换手,...;..."})
       全部历史：把 {year} 换成 all.js（压缩格式，需按 sortYear 展开，本模块用逐年接口更稳）
  2. 实时行情  http://d.10jqka.com.cn/v6/realhead/hs_{code}/last.js
  3. 分时      http://d.10jqka.com.cn/v6/time/hs_{code}/last.js
  4. 涨幅榜    http://q.10jqka.com.cn/index/index/board/all/field/zdf/order/desc/page/{n}/ajax/1/
       ⚠ 该站有反爬：仅第 1 页可取（20 只），第 2 页起返回 401/403（需 hexin-v 动态 token）。
       因此全市场列表仍由东财 clist 提供（见 data_source.py），同花顺负责 K 线与实时行情。

字段口径（日 K，实测与东财前复权一致）：
    日期, 开盘, 最高, 最低, 收盘, 成交量(手), 成交额(元), 换手率%, ...
    注意：与东财 kline 的「开,收,高,低」顺序不同，此处为「开,高,低,收」。

线程安全：每线程独立 Session；节点失败降权；403/401 不重试（反爬，立刻放弃）。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

import config

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

D_KLINE = "http://d.10jqka.com.cn/v6/line/{sym}/01/{year}.js"
D_ALL = "http://d.10jqka.com.cn/v6/line/{sym}/01/all.js"
D_QUOTE = "http://d.10jqka.com.cn/v6/realhead/{sym}/last.js"
D_TIME = "http://d.10jqka.com.cn/v6/time/{sym}/last.js"
Q_RANK = ("http://q.10jqka.com.cn/index/index/board/all/field/{field}"
          "/order/{order}/page/{page}/ajax/1/")

_JSONP = re.compile(r"\((\{.*\})\)\s*;?\s*$", re.S)
_TAG = re.compile(r"<[^>]+>")

_local = threading.local()


class ThsError(RuntimeError):
    """同花顺数据源异常。"""


def _session() -> requests.Session:
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        s.trust_env = False        # 忽略系统代理（同花顺直连即可）
        s.headers.update({
            "User-Agent": random.choice(_UA_POOL),
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "http://stockpage.10jqka.com.cn/",
            "Connection": "keep-alive",
        })
        _local.s = s
    return s


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if s in ("", "-", "--", "null", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def to_ths_symbol(code: str) -> str:
    """A 股代码 → 同花顺符号。实测 hs_ 前缀对沪/深/创/科/北交所通用。"""
    c = re.sub(r"\D", "", str(code or ""))
    return "hs_%s" % c


def secid_to_code(secid: str) -> str:
    """东财 secid（如 1.600519）→ 纯代码（600519）。"""
    return str(secid or "").split(".")[-1]


def _get_text(url: str, retry: int = None, timeout: int = None) -> str:
    """GET 文本，带重试 + UA 轮换。401/403（反爬）不重试，直接抛错。"""
    retry = retry or config.THS_HTTP_RETRY
    timeout = timeout or config.HTTP_TIMEOUT
    last = None
    for attempt in range(retry):
        try:
            r = _session().get(url, timeout=timeout)
            if r.status_code == 200:
                return r.text
            if r.status_code in (401, 403):
                raise ThsError("同花顺接口反爬拦截（HTTP %s）: %s" % (r.status_code, url))
            last = ThsError("HTTP %s" % r.status_code)
        except ThsError:
            raise
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(min(0.3 * (2 ** attempt), 2.0) + random.random() * 0.3)
    raise ThsError("请求失败 %s: %s" % (url, last))


def _jsonp(text: str):
    m = _JSONP.search(text.strip())
    if not m:
        m = re.search(r"\((\{.*\})\)", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 1. 日 K 线
# ---------------------------------------------------------------------------
def _parse_kline_csv(data: str, prev_close: float = None) -> list:
    """解析同花顺日K CSV 串 → [{date, open, close, high, low, volume, amount, pct}]。

    CSV 字段顺序：日期,开,高,低,收,量(手),额(元),换手%,...
    pct 由相邻收盘价自行计算（接口给的并非涨跌幅），首条缺失则由 prev_close 推算。
    """
    rows = []
    for chunk in (data or "").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        p = chunk.split(",")
        if len(p) < 6:
            continue
        d = p[0].strip()
        if len(d) != 8 or not d.isdigit():
            continue
        o, h, l, c = _num(p[1]), _num(p[2]), _num(p[3]), _num(p[4])
        if c is None:
            continue
        rows.append({
            "date": "%s-%s-%s" % (d[:4], d[4:6], d[6:]),
            "open": o, "close": c, "high": h, "low": l,
            "volume": _num(p[5]) if len(p) > 5 else None,
            "amount": _num(p[6]) if len(p) > 6 else None,
            "turnover": _num(p[7]) if len(p) > 7 else None,
            "pct": None,
        })
    if not rows:
        return []
    # 计算涨跌幅
    for i, r in enumerate(rows):
        base = rows[i - 1]["close"] if i > 0 else prev_close
        if base:
            r["pct"] = round((r["close"] / base - 1.0) * 100.0, 4)
    return rows


def fetch_kline_year(code: str, year: int) -> list:
    """抓取某一年度的日 K。无数据（未上市/停牌年份）返回空列表。"""
    url = D_KLINE.format(sym=to_ths_symbol(code), year=year)
    try:
        payload = _jsonp(_get_text(url))
    except ThsError:
        return []
    if not payload:
        return []
    return _parse_kline_csv(payload.get("data") or "")


def fetch_kline(code: str, lookback_days: int = None, refresh: bool = False) -> list:
    """获取单只股票近 lookback_days 自然日日 K（同花顺，逐年拼接），带本地缓存。

    返回结构对齐 data_source.fetch_kline：list[{date, open, close, high, low, volume, amount, pct}]
    """
    lookback_days = lookback_days or config.KLINE_LOOKBACK_DAYS
    path = _kline_cache_path(code)
    if not refresh and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                cached = json.load(fh)
            if cached.get("fetched_at", "")[:10] == dt.date.today().strftime("%Y-%m-%d") and cached.get("rows"):
                return cached["rows"]
        except Exception:  # noqa: BLE001
            pass

    today = dt.date.today()
    start = today - dt.timedelta(days=lookback_days + 10)
    years = list(range(today.year, start.year - 1, -1))

    all_rows = []
    for y in years:
        rows = fetch_kline_year(code, y)
        if rows:
            all_rows = rows + all_rows
        if all_rows and len(all_rows) >= lookback_days:
            break

    # 去重 + 按日期升序 + 截取窗口
    seen, merged = set(), []
    for r in all_rows:
        if r["date"] in seen:
            continue
        seen.add(r["date"])
        merged.append(r)
    merged.sort(key=lambda r: r["date"])
    cutoff = start.strftime("%Y-%m-%d")
    merged = [r for r in merged if r["date"] >= cutoff]

    if merged:
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({
                    "code": code, "source": "ths",
                    "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
                    "rows": merged,
                }, fh, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass
    return merged


def fetch_klines_bulk(codes, refresh: bool = False, on_progress=None, workers=None):
    """并发批量抓 K 线，返回 {code: rows}。单只失败不影响整体，失败项补抓 1 轮。"""
    workers = workers or config.THS_KLINE_WORKERS
    out = {c: [] for c in codes}
    total = len(codes)
    done = 0
    pending = list(codes)
    for attempt in range(2):
        if not pending:
            break
        if attempt:
            time.sleep(1.2)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(fetch_kline, c, None, refresh): c for c in pending}
            for fut in as_completed(futs):
                code = futs[fut]
                try:
                    rows = fut.result()
                except Exception:  # noqa: BLE001
                    rows = []
                if rows:
                    out[code] = rows
                done += 1
                if on_progress and (done % 10 == 0 or done == total):
                    on_progress(min(done, total), total)
        pending = [c for c in pending if not out[c]]
    return out


def _kline_cache_path(code: str) -> str:
    return os.path.join(config.KLINE_DIR, "ths_%s.json" % re.sub(r"\D", "", str(code)))


# ---------------------------------------------------------------------------
# 2. 实时行情（realhead）
# ---------------------------------------------------------------------------
# 字段映射说明（实测比对贵州茅台/平安银行后确认）：
#   10=最新价  6=昨收  7=今开  8=最高  9=最低  13=成交量(手)  19=成交额(元)
#   199112=涨跌幅%  264648=涨跌额  2942=市盈率TTM  2034120=市盈率(静)
#   3475914=总市值  3541450=流通市值  1968584=量比  127=名称占位
_REALTIME_MAP = {
    "price": "10", "prev_close": "6", "open": "7", "high": "8", "low": "9",
    "volume": "13", "amount": "19", "pct_chg": "199112", "chg": "264648",
    "pe": "2942", "total_mv": "3475914", "circ_mv": "3541450",
}


def fetch_quote(code: str) -> dict:
    """同花顺实时行情快照。失败返回 {}，不抛异常。"""
    url = D_QUOTE.format(sym=to_ths_symbol(code))
    try:
        payload = _jsonp(_get_text(url, retry=2, timeout=8))
    except ThsError:
        return {}
    items = (payload or {}).get("items") or {}
    if not items:
        return {}
    out = {"code": re.sub(r"\D", "", str(code)), "source": "ths"}
    out["name"] = items.get("name")
    for k, f in _REALTIME_MAP.items():
        out[k] = _num(items.get(f))
    out["quote_time"] = items.get("time")
    return out


def fetch_quotes_bulk(codes, workers: int = 12):
    """并发批量实时行情，返回 {code: quote}。"""
    out = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(fetch_quote, c): c for c in codes}
        for fut in as_completed(futs):
            try:
                q = fut.result()
            except Exception:  # noqa: BLE001
                q = {}
            if q:
                out[futs[fut]] = q
    return out


# ---------------------------------------------------------------------------
# 3. 分时
# ---------------------------------------------------------------------------
def fetch_time_shares(code: str) -> dict:
    """当日分时数据（同花顺 time 接口）。"""
    url = D_TIME.format(sym=to_ths_symbol(code))
    try:
        payload = _jsonp(_get_text(url, retry=2, timeout=8))
    except ThsError:
        return {}
    node = (payload or {}).get(to_ths_symbol(code)) or {}
    return node or {}


# ---------------------------------------------------------------------------
# 4. 涨幅榜（注意：同花顺排行页仅第 1 页可取，多页需 hexin-v，会自动回退为空）
# ---------------------------------------------------------------------------
_RANK_COLS = ["seq", "code", "name", "price", "pct_chg", "chg", "speed",
              "turnover", "volume_ratio", "amplitude", "amount", "circ_shares",
              "circ_mv", "pe"]


def fetch_rank(field: str = "zdf", order: str = "desc", page: int = 1) -> list:
    """同花顺全市场排行页（field: zdf 涨跌幅 / zdf60 / cje 成交额 等）。

    ⚠ 仅 page=1 稳定可用；page>=2 会被反爬拦截（HTTP 401/403），此时返回 []。
    """
    url = Q_RANK.format(field=field, order=order, page=page)
    try:
        html = _get_text(url, retry=1, timeout=10)
    except ThsError:
        return []
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        tds = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.S)
        vals = [_TAG.sub("", t).strip() for t in tds]
        if len(vals) < len(_RANK_COLS) or not vals[1].isdigit():
            continue          # 表头行 / 非数据行
        rows.append(dict(zip(_RANK_COLS, vals)))
    return rows


def extract_stock_list(page: int = 1) -> list:
    """从排行页抽股票代码列表（供自选/热点辅助）。仅第 1 页可用。"""
    return [{"code": r["code"], "name": r["name"]} for r in fetch_rank(page=page)
            if re.match(r"^\d{6}$", r.get("code", ""))]


# ---------------------------------------------------------------------------
# 5. 连通性自检
# ---------------------------------------------------------------------------
def health_check(code: str = "600519") -> dict:
    """各接口连通性自检，返回 {接口: 状态}。"""
    res = {}
    t0 = time.time()
    try:
        rows = fetch_kline_year(code, dt.date.today().year)
        res["kline"] = {"ok": bool(rows), "rows": len(rows),
                        "last": rows[-1]["date"] if rows else None}
    except Exception as exc:  # noqa: BLE001
        res["kline"] = {"ok": False, "error": str(exc)}
    q = fetch_quote(code)
    res["quote"] = {"ok": bool(q), "name": q.get("name"), "price": q.get("price"),
                    "pct_chg": q.get("pct_chg")}
    res["rank_page1"] = {"ok": bool(fetch_rank()), "count": len(fetch_rank())}
    res["elapsed_sec"] = round(time.time() - t0, 2)
    return res
