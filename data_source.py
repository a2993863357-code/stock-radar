# -*- coding: utf-8 -*-
"""
data_source.py —— 行情数据源统一入口（同花顺 / 东方财富双源 + 本地缓存）。

数据源分工（config.DATA_SOURCE 控制，默认 ths）：
  * 全市场快照   —— 东财 clist（同花顺排行页有反爬，第 2 页起 401，无法全量）
  * 日 K 线      —— 同花顺 d.10jqka.com.cn（逐年，默认）；可回退东财 push2his
  * 个股实时行情 —— 同花顺 realhead；可回退东财 push2
  * 股票检索     —— 本地快照匹配（代码/名称/拼音），东财 suggest 兜底

职责：
  1. 全市场快照抓取（push2.eastmoney.com/api/qt/clist/get，分页拉全量）
  2. 个股日 K 线抓取（同花顺逐年 / 东财 klt=101 fqt=1）
  3. 个股实时行情（同花顺 realhead / 东财 stock/get）
  4. 股票检索（本地快照匹配：代码 / 名称 / 拼音首字母；东财 suggest 兜底）
  5. 缓存读写与缓存新鲜度判断

依赖：requests、pandas（可选，仅用于本地分析）、标准库。
不依赖任何 API Key，全部为公开行情接口。
"""
from __future__ import annotations

import json
import os
import random
import re
import time
import datetime as dt
import itertools
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

import config
import data_source_ths as _ths

# 回退事件记录（同花顺 → 东财），最多保留最近 50 条，供 /api/status 展示
_FALLBACKS = []
_FALLBACK_LOCK = threading.Lock()


def _log_fallback(kind: str, key: str, reason) -> None:
    """记录一次数据源回退事件（不抛异常、不阻塞主流程）。"""
    try:
        with _FALLBACK_LOCK:
            _FALLBACKS.append({
                "at": dt.datetime.now().isoformat(timespec="seconds"),
                "kind": kind, "key": str(key), "reason": str(reason)[:180],
            })
            del _FALLBACKS[:-50]
    except Exception:  # noqa: BLE001
        pass


def fallback_events() -> list:
    with _FALLBACK_LOCK:
        return list(_FALLBACKS)

# ---------------------------------------------------------------------------
# HTTP 基础设施
# ---------------------------------------------------------------------------
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://quote.eastmoney.com/",
    "Connection": "keep-alive",
}

_local = threading.local()


def _session() -> requests.Session:
    """每个线程一个 Session，复用连接。

    注意：Windows 上若配置了系统级代理（如 127.0.0.1:7897，常见于 Clash 等工具），
    requests 会自动读取该代理；一旦代理未开启，所有请求都会抛 ProxyError。
    东财接口在国内直连即可访问，因此这里关闭 trust_env（忽略环境变量 + 注册表代理），
    仅在直连彻底失败时再回退到代理（见 _get_json 末尾）。
    """
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update(_HEADERS)
        s.trust_env = False
        _local.session = s
    return s


class DataSourceError(RuntimeError):
    """数据源异常。"""


_host_counter = itertools.count()
_host_lock = threading.Lock()
# 节点健康度：host -> 连续失败次数。连续失败达到阈值后自动从轮询池剔除（成功即归零），
# 避免把请求反复打到不可用镜像上 —— 本机网络下 push2his 多数节点会被直接重置连接。
_host_fail = {}
_host_ok = {}
_HOST_FAIL_MAX = 3


def _host_pool(url: str):
    """判断该 URL 是否需要走镜像节点池。

    - K 线接口（push2his 系）→ HOSTS_KLINE
    - 其余 push2 系行情接口 → HOSTS_CLIST
    - 其它域名（如 searchapi.eastmoney.com 检索接口）→ None，保持原域名不重写
    """
    parts = urllib.parse.urlsplit(url)
    path, host = parts.path, (parts.hostname or "")
    if "kline" in path.lower():
        return config.HOSTS_KLINE
    if "push2" in host:
        return config.HOSTS_CLIST
    return None


def _next_host(pool):
    """取下一个可用节点（多线程安全）。

    两段筛选：
      1) 剔除连败节点（失败达 _HOST_FAIL_MAX 次）；
      2) 若已有节点被实测验证可用，则只在「已验证节点」之间轮询。
    第 2 步是本机网络下的提速关键 —— 盲目轮询会把大量请求丢给失效镜像，
    每次都要等完整重试退避（约 10s），实测会把 800 只股票拖到 9 分钟以上。
    """
    with _host_lock:
        healthy = [h for h in pool if _host_fail.get(h, 0) < _HOST_FAIL_MAX]
        cand = healthy or list(pool)
        proven = [h for h in cand if _host_ok.get(h, 0) > 0 and _host_fail.get(h, 0) == 0]
        if proven:
            cand = proven
        return cand[next(_host_counter) % len(cand)]


def _mark_host(host, ok: bool) -> None:
    """记录节点可用性，用于自动剔除长期不可用的镜像。"""
    if not host:
        return
    with _host_lock:
        if ok:
            _host_fail[host] = 0
            _host_ok[host] = _host_ok.get(host, 0) + 1
        else:
            _host_fail[host] = _host_fail.get(host, 0) + 1


def _get_json(url: str, params: dict, retry: int = None, timeout: int = None):
    """带节点切换 + 重试的 GET，返回解析后的 JSON，全部失败时抛 DataSourceError。

    本机网络的两个已知问题，这里同时兜住：
      1) push2his 多数镜像节点会被直接重置连接 —— 节点池轮询 + 失败降权规避；
      2) 系统级代理常处于未开启状态，走代理必失败 —— 代理只作为最后一次兜底。
    """
    retry = retry or config.HTTP_RETRY
    timeout = timeout or config.HTTP_TIMEOUT
    path = urllib.parse.urlsplit(url).path
    pool = _host_pool(url)
    proxies = requests.utils.getproxies()
    last_err = None
    for attempt in range(retry):
        host = _next_host(pool) if pool else None
        target = ("https://%s%s" % (host, path)) if pool else url
        # 代理只作为最后一次兜底：系统代理未开启时过早走代理只会白白浪费重试次数。
        use_proxy = bool(proxies) and attempt == retry - 1
        try:
            if use_proxy:
                resp = requests.get(target, params=params, timeout=timeout,
                                    headers=_HEADERS, proxies=proxies)
            else:
                resp = _session().get(target, params=params, timeout=timeout)
            if resp.status_code == 200:
                # 重要：东财接口在「标的确实无数据」（新股/停牌/退市）时同样返回
                # 200 + 空 klines，这属于确定性业务结果，不是节点故障。此处一律
                # 视为请求成功，由 fetch_kline 返回空列表。早期版本把它当节点失效
                # 处理，会反复误杀健康节点，请求退回失效节点池，单只耗时从 0.3s
                # 劣化到 8s 以上（实测 800 只候选池要 9 分钟）。
                _mark_host(host, True)
                return resp.json()
            last_err = DataSourceError("HTTP %s @%s" % (resp.status_code, target))
            _mark_host(host, False)
        except Exception as exc:  # noqa: BLE001 - 网络异常统一重试
            last_err = exc
            _mark_host(host, False)
        time.sleep(min(0.25 * (2 ** attempt), 2.0) + random.random() * 0.3)
    raise DataSourceError("请求失败 %s: %s" % (path, last_err))


def _num(v):
    """东财用 '-' 表示无数据，统一转成 float 或 None。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if s in ("", "-", "--"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _today_str() -> str:
    return dt.date.today().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 1. 全市场快照
# ---------------------------------------------------------------------------
def _parse_snapshot_item(d: dict) -> dict:
    return {
        "code": str(d.get("f12") or "").strip(),
        "market": d.get("f13"),
        "secid": "%s.%s" % (d.get("f13"), d.get("f12")),
        "name": (d.get("f14") or "").strip(),
        "price": _num(d.get("f2")),
        "pct_chg": _num(d.get("f3")),          # 今日涨跌幅 %
        "amount": _num(d.get("f6")),           # 成交额（元）
        "turnover": _num(d.get("f8")),         # 换手率 %
        "pe": _num(d.get("f9")),               # 市盈率(动)
        "volume_ratio": _num(d.get("f10")),    # 量比
        "total_mv": _num(d.get("f20")),        # 总市值（元）
        "circ_mv": _num(d.get("f21")),         # 流通市值（元）
        "pb": _num(d.get("f23")),              # 市净率
        "chg_60d": _num(d.get("f24")),         # 60 日涨跌幅 %
        "chg_ytd": _num(d.get("f25")),         # 年初至今涨跌幅 %
        "chg_5d": _num(d.get("f109")),         # 5 日涨跌幅 %
        "list_date": str(d.get("f26") or ""),
        "industry": (d.get("f100") or "").strip(),
        "main_inflow": _num(d.get("f62")),     # 主力净流入（元）
    }


def fetch_snapshot(on_progress=None) -> list:
    """分页拉取沪深京 A 股全市场快照，返回 list[dict]。

    先取第 1 页拿到 total，再并发拉取剩余页，最后按代码去重。
    """
    base = {
        "po": 1,
        "np": 1,
        "fltt": 2,
        "invt": 2,
        "fid": "f3",
        "fs": config.MARKET_FS,
        "fields": config.SNAPSHOT_FIELDS,
        "pz": config.PAGE_SIZE,
    }

    first = _get_json(config.CLIST_URL, dict(base, pn=1))
    data = (first or {}).get("data") or {}
    total = int(data.get("total") or 0)
    items = list(data.get("diff") or [])
    if not total:
        raise DataSourceError("快照首页无数据，接口可能已变动或网络不可用")

    pages = (total + config.PAGE_SIZE - 1) // config.PAGE_SIZE
    if on_progress:
        on_progress(1, pages)

    def _one(pn):
        return _get_json(config.CLIST_URL, dict(base, pn=pn))

    done = 1
    # 缺失分页补抓：网络间歇性阻断时单页可能整页丢失，这里最多再补 2 轮
    missing = list(range(2, pages + 1))
    for attempt in range(3):
        if not missing:
            break
        if attempt:
            time.sleep(1.5 * attempt)
        failed = []
        with ThreadPoolExecutor(max_workers=config.PAGE_WORKERS) as pool:
            futs = {pool.submit(_one, pn): pn for pn in missing}
            for fut in as_completed(futs):
                pn = futs[fut]
                try:
                    got = fut.result()
                except Exception:  # noqa: BLE001 - 单页失败不致命
                    got = None
                rows = ((got or {}).get("data") or {}).get("diff") or []
                if rows:
                    items.extend(rows)
                else:
                    failed.append(pn)
                if not attempt:
                    done += 1
                    if on_progress:
                        on_progress(min(done, pages), pages)
        missing = failed
    if missing:
        print("[warn] %d 个快照分页多次重试后仍失败，结果可能不完整: %s"
              % (len(missing), missing[:20]))

    seen, cleaned = set(), []
    for d in items:
        row = _parse_snapshot_item(d)
        if not row["code"] or row["code"] in seen:
            continue
        seen.add(row["code"])
        cleaned.append(row)
    return cleaned


# ---------------------------------------------------------------------------
# 2. 日 K 线
# ---------------------------------------------------------------------------
def _kline_cache_path(secid: str) -> str:
    """东财 K 线缓存路径（em_ 前缀，与同花顺 ths_ 前缀区分，互不覆盖）。"""
    safe = secid.replace(".", "_")
    return os.path.join(config.KLINE_DIR, "em_%s.json" % safe)


def fetch_kline(secid: str, refresh: bool = False) -> list:
    """获取单只股票近 KLINE_LOOKBACK_DAYS 天日 K，按 config.KLINE_SOURCE 路由数据源。

    - ths（默认）：同花顺 d.10jqka.com.cn 逐年日 K
    - eastmoney  ：东财 push2his（原实现，保留可回退）
    返回结构统一为 list[dict]：{date, open, close, high, low, volume, amount, pct}
    """
    if config.KLINE_SOURCE == "ths":
        code = _ths.secid_to_code(secid)
        if code:
            try:
                rows = _ths.fetch_kline(code, config.KLINE_LOOKBACK_DAYS, refresh)
            except Exception as exc:  # noqa: BLE001 - 同花顺不可用时静默回退东财
                _log_fallback("K线", secid, exc)
                rows = []
            if rows:
                return rows
            _log_fallback("K线", secid, "同花顺返回空数据")
    return fetch_kline_em(secid, refresh)


def fetch_kline_em(secid: str, refresh: bool = False) -> list:
    """东财日 K（前复权）原实现。

    说明：必须带 beg 限制回溯区间——若从上市首日开始取前复权，早期价格会因累计
    复权因子出现负值失真；限定近区间可保证价格与当前行情一致。
    """
    path = _kline_cache_path(secid)
    if not refresh and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                cached = json.load(fh)
            if cached.get("fetched_at", "")[:10] == _today_str() and cached.get("rows"):
                return cached["rows"]
        except Exception:  # noqa: BLE001 - 缓存损坏则重新抓取
            pass

    beg = (dt.date.today() - dt.timedelta(days=config.KLINE_LOOKBACK_DAYS)).strftime("%Y%m%d")
    params = {
        "secid": secid,
        "klt": 101,          # 日 K
        "fqt": 1,            # 前复权
        "beg": beg,
        "end": "20500101",
        "lmt": 1000,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
    }
    payload = _get_json(config.KLINE_URL, params)
    data = (payload or {}).get("data") or {}
    rows = []
    for line in data.get("klines") or []:
        p = line.split(",")
        if len(p) < 8:
            continue
        close = _num(p[2])
        if close is None:
            continue
        rows.append(
            {
                "date": p[0],
                "open": _num(p[1]),
                "close": close,
                "high": _num(p[3]),
                "low": _num(p[4]),
                "volume": _num(p[5]),
                "amount": _num(p[6]),
                "pct": _num(p[7]),
            }
        )
    if rows:
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(
                    {"secid": secid, "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
                     "rows": rows},
                    fh, ensure_ascii=False,
                )
        except Exception:  # noqa: BLE001
            pass
    return rows


def fetch_klines_bulk(secids, refresh: bool = False, on_progress=None, workers=None):
    """并发批量抓取 K 线，返回 {secid: rows}，按 config.KLINE_SOURCE 路由数据源。

    - ths：先走同花顺（并发 24，单只按年请求）；返回空数据的个股自动回退东财补齐
    - eastmoney：全部走东财
    网络间歇性阻断会导致部分请求整只失败，因此失败项会再补抓 1 轮。
    """
    secids = list(secids)
    if config.KLINE_SOURCE == "ths":
        out = _fetch_klines_bulk_ths(secids, refresh, on_progress, workers)
        miss = [s for s in secids if not out.get(s)]
        if miss:
            _log_fallback("日K批量", "%d/%d 只" % (len(miss), len(secids)),
                          "同花顺未返回数据，回退东财补齐")
            em = _fetch_klines_bulk_em(miss, refresh, None, workers)
            for s, rows in (em or {}).items():
                if rows:
                    out[s] = rows
        return out
    return _fetch_klines_bulk_em(secids, refresh, on_progress, workers)


def _fetch_klines_bulk_ths(secids, refresh=False, on_progress=None, workers=None):
    """同花顺批量 K 线（secid → 代码 → 结果映射回 secid）。"""
    mp = {s: _ths.secid_to_code(s) for s in secids}
    codes = [c for c in mp.values() if c]
    if not codes:
        return {s: [] for s in secids}
    rows_by_code = _ths.fetch_klines_bulk(
        codes, refresh=refresh, on_progress=on_progress,
        workers=workers or config.THS_KLINE_WORKERS,
    )
    return {s: (rows_by_code.get(c) or []) for s, c in mp.items()}


def _fetch_klines_bulk_em(secids, refresh: bool = False, on_progress=None, workers=None):
    """东财批量 K 线（原实现）。"""
    workers = workers or config.KLINE_WORKERS
    out = {s: [] for s in secids}
    total = len(secids)
    done = 0
    pending = list(secids)
    for attempt in range(2):
        if not pending:
            break
        if attempt:
            time.sleep(1.5)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(fetch_kline_em, s, refresh): s for s in pending}
            for fut in as_completed(futs):
                secid = futs[fut]
                try:
                    rows = fut.result()
                except Exception:  # noqa: BLE001
                    rows = []
                if rows:
                    out[secid] = rows
                done += 1
                if on_progress and (done % 10 == 0 or done == total):
                    on_progress(min(done, total), total)
        pending = [s for s in pending if not out[s]]
    return out


# ---------------------------------------------------------------------------
# 2.5 个股实时行情
# ---------------------------------------------------------------------------
def fetch_quote(secid: str) -> dict:
    """个股实时行情，按 config.QUOTE_SOURCE 路由。

    同花顺 realhead 字段：最新价/昨收/今开/最高/最低/成交量(手)/成交额(元)/
    涨跌幅/涨跌额/市盈率/总市值/流通市值。失败自动回退东财。
    """
    if config.QUOTE_SOURCE == "ths":
        code = _ths.secid_to_code(secid)
        if code:
            try:
                q = _ths.fetch_quote(code)
            except Exception as exc:  # noqa: BLE001
                _log_fallback("实时行情", secid, exc)
                q = {}
            if q and q.get("price") is not None:
                q["secid"] = secid
                return q
            _log_fallback("实时行情", secid, "同花顺未返回行情")
        else:
            _log_fallback("实时行情", secid, "无法解析代码")
    return fetch_quote_em(secid)


def fetch_quote_em(secid: str) -> dict:
    """东财个股实时行情（兜底）。"""
    params = {
        "secid": secid, "invt": 2, "fltt": 2,
        "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f60,f116,f117,f162,f169,f170",
    }
    try:
        payload = _get_json(config.QUOTE_URL, params)
    except Exception as exc:  # noqa: BLE001
        _log_fallback("实时行情", secid, exc)
        return {}
    d = (payload or {}).get("data") or {}
    if not d:
        return {}
    scale = 100.0
    return {
        "code": _ths.secid_to_code(secid), "secid": secid, "source": "eastmoney",
        "name": d.get("f58"),
        "price": _num(d.get("f43")) / scale if d.get("f43") is not None else None,
        "high": _num(d.get("f44")) / scale if d.get("f44") is not None else None,
        "low": _num(d.get("f45")) / scale if d.get("f45") is not None else None,
        "open": _num(d.get("f46")) / scale if d.get("f46") is not None else None,
        "prev_close": _num(d.get("f60")) / scale if d.get("f60") is not None else None,
        "volume": _num(d.get("f47")),          # 手
        "amount": _num(d.get("f48")),          # 元
        "pct_chg": _num(d.get("f170")),        # %
        "chg": _num(d.get("f169")) / scale if d.get("f169") is not None else None,
        "pe": _num(d.get("f162")),
        "total_mv": _num(d.get("f116")),
        "circ_mv": _num(d.get("f117")),
    }


# ---------------------------------------------------------------------------
# 3. 股票检索
# ---------------------------------------------------------------------------
# 汉字 -> 拼音首字母（GBK 编码区间法，覆盖 GB2312 一级常用汉字）
_INITIAL_SECTIONS = [
    (0xB0A1, 0xB0C4, "A"), (0xB0C5, 0xB2C0, "B"), (0xB2C1, 0xB4ED, "C"),
    (0xB4EE, 0xB6E9, "D"), (0xB6EA, 0xB7A1, "E"), (0xB7A2, 0xB8C0, "F"),
    (0xB8C1, 0xB9FD, "G"), (0xB9FE, 0xBBF6, "H"), (0xBBF7, 0xBFA5, "J"),
    (0xBFA6, 0xC0AB, "K"), (0xC0AC, 0xC2E7, "L"), (0xC2E8, 0xC4C2, "M"),
    (0xC4C3, 0xC5B5, "N"), (0xC5B6, 0xC5BD, "O"), (0xC5BE, 0xC6D9, "P"),
    (0xC6DA, 0xC8BA, "Q"), (0xC8BB, 0xC8F5, "R"), (0xC8F6, 0xCBF9, "S"),
    (0xCBFA, 0xCDD9, "T"), (0xCDDA, 0xCEF3, "W"), (0xCEF4, 0xD188, "X"),
    (0xD1B9, 0xD4D0, "Y"), (0xD4D1, 0xD7F9, "Z"),
]


def _char_initial(ch: str) -> str:
    try:
        raw = ch.encode("gbk")
    except Exception:  # noqa: BLE001
        return ""
    if len(raw) < 2:
        return ch.upper() if ch.isascii() else ""
    code = raw[0] * 256 + raw[1]
    for lo, hi, letter in _INITIAL_SECTIONS:
        if lo <= code <= hi:
            return letter
    return ""


def pinyin_initials(text: str) -> str:
    """取文本的拼音首字母（含首字母后仍保留原字符，便于二次匹配）。"""
    return "".join(_char_initial(c) for c in (text or ""))


def _name_index(name: str) -> str:
    """名称的检索索引：拼音首字母 + 名称本身（去掉 ST/空格等噪声）。"""
    base = re.sub(r"[\s*]", "", name or "")
    base = re.sub(r"^(ST|st|\*ST)", "", base)
    return (pinyin_initials(base) + "|" + base).upper()


def search_stocks(keyword: str, snapshot: list, limit: int = 20, use_remote: bool = True) -> list:
    """按代码 / 名称 / 拼音首字母检索股票。

    先在本地全市场快照中匹配（快且不依赖网络）；未命中时用东财 suggest 接口兜底。
    """
    kw = (keyword or "").strip()
    if not kw:
        return []
    kw_up = kw.upper()
    exact, prefix, contains = [], [], []
    for s in snapshot or []:
        code = s.get("code", "")
        name = s.get("name", "")
        if code == kw:
            exact.append(s)
            continue
        if code.startswith(kw) or name.upper().startswith(kw_up):
            prefix.append(s)
            continue
        if kw_up in _name_index(name) or kw in name or (kw.isdigit() and kw in code):
            contains.append(s)
    result = exact + prefix + contains
    if not result and use_remote:
        result = _remote_suggest(kw, snapshot)
    return result[:limit]


def _remote_suggest(keyword: str, snapshot: list) -> list:
    """东财 suggest 接口兜底（该接口不稳定，失败则返回空列表）。"""
    try:
        payload = _get_json(
            config.SUGGEST_URL,
            {"input": keyword, "type": 14, "token": config.SUGGEST_TOKEN, "count": 10},
        )
    except Exception:  # noqa: BLE001
        return []
    table = ((payload or {}).get("QuotationCodeTable") or {}).get("Data") or []
    idx = {s["code"]: s for s in snapshot or []}
    out = []
    for it in table:
        code = str(it.get("Code") or "")
        if not code:
            continue
        if code in idx:
            out.append(idx[code])
        else:
            out.append(
                {
                    "code": code, "name": it.get("Name") or "", "secid": it.get("QuoteID") or "",
                    "market": (it.get("QuoteID") or ".")[0] if it.get("QuoteID") else None,
                    "price": None, "pct_chg": None, "amount": None, "turnover": None,
                    "pe": None, "volume_ratio": None, "total_mv": None, "circ_mv": None,
                    "pb": None, "chg_60d": None, "chg_ytd": None, "chg_5d": None,
                    "list_date": "", "industry": it.get("SecurityTypeName") or "",
                    "main_inflow": None,
                }
            )
    return out


# ---------------------------------------------------------------------------
# 4. 缓存读写
# ---------------------------------------------------------------------------
def _write_json(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)
    os.replace(tmp, path)


def _read_json(path: str, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return default


def load_snapshot() -> list:
    obj = _read_json(config.SNAPSHOT_FILE) or {}
    return obj.get("stocks") or []


def save_snapshot(stocks: list, extra: dict = None) -> dict:
    payload = {
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "trade_date": _today_str(),
        "count": len(stocks),
        "source": "eastmoney push2 clist",
        "source_reason": "同花顺排行页有反爬（第2页起401），全市场列表由东财提供",
    }
    if extra:
        payload.update(extra)
    _write_json(config.SNAPSHOT_FILE, {**payload, "stocks": stocks})
    return payload


def save_scores(items: list, meta: dict = None) -> dict:
    src = "同花顺 d.10jqka.com.cn 日K" if config.KLINE_SOURCE == "ths" else "东财 push2his 日K"
    payload = {
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "trade_date": _today_str(),
        "count": len(items),
        "source": "%s + 本地评分模型" % src,
        "kline_source": config.KLINE_SOURCE,
        "fallbacks": fallback_events()[-10:],
    }
    if meta:
        payload.update(meta)
    _write_json(config.SCORE_FILE, {**payload, "items": items})
    return payload


def load_scores_payload() -> dict:
    return _read_json(config.SCORE_FILE) or {}


def load_meta() -> dict:
    return _read_json(config.META_FILE) or {}


def save_meta(meta: dict) -> None:
    _write_json(config.META_FILE, meta)


def cache_age_hours() -> float:
    """缓存距今小时数；无缓存返回 inf。"""
    obj = _read_json(config.SNAPSHOT_FILE) or {}
    ts = obj.get("updated_at")
    if not ts:
        return float("inf")
    try:
        when = dt.datetime.fromisoformat(ts)
    except ValueError:
        return float("inf")
    return (dt.datetime.now() - when).total_seconds() / 3600.0


def cache_is_fresh() -> bool:
    return cache_age_hours() <= config.CACHE_MAX_AGE_HOURS


def cache_status() -> dict:
    snap = _read_json(config.SNAPSHOT_FILE) or {}
    sc = load_scores_payload()
    files = os.listdir(config.KLINE_DIR) if os.path.isdir(config.KLINE_DIR) else []
    return {
        "snapshot_updated_at": snap.get("updated_at"),
        "snapshot_count": snap.get("count", 0),
        "snapshot_source": snap.get("source"),
        "scores_updated_at": sc.get("updated_at"),
        "scores_count": sc.get("count", 0),
        "scores_source": sc.get("source"),
        "pool_size": sc.get("pool_size", config.POOL_SIZE),
        "age_hours": None if cache_age_hours() == float("inf") else round(cache_age_hours(), 2),
        "fresh": cache_is_fresh(),
        "kline_cache_files": len(files),
        # 分源统计缓存文件（em_ 东财 / ths_ 同花顺）
        "kline_cache_by_source": {
            "ths": len([f for f in files if f.startswith("ths_")]),
            "eastmoney": len([f for f in files if f.startswith("em_")]),
            "legacy": len([f for f in files if not f.startswith(("ths_", "em_"))]),
        },
        "kline_source": config.KLINE_SOURCE,
        "quote_source": config.QUOTE_SOURCE,
        "snapshot_source_config": config.SNAPSHOT_SOURCE,
        "recent_fallbacks": fallback_events()[-10:],
    }
