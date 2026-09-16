# -*- coding: utf-8 -*-
"""
Stock Radar 全局配置。

所有可调参数集中在此，便于按需修改（评分权重、候选池大小、并发数、定时刷新时间等）。
也可通过环境变量覆盖部分关键参数，便于用 Windows 计划任务做无人值守刷新。
"""
import os

# ---------------------------------------------------------------------------
# 基础路径
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
KLINE_DIR = os.path.join(DATA_DIR, "kline_cache")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(KLINE_DIR, exist_ok=True)

SNAPSHOT_FILE = os.path.join(DATA_DIR, "snapshot.json")
SCORE_FILE = os.path.join(DATA_DIR, "scores.json")
META_FILE = os.path.join(DATA_DIR, "meta.json")
REFRESH_LOG_FILE = os.path.join(DATA_DIR, "refresh.log")

# ---------------------------------------------------------------------------
# 数据源路由（重要）
# ---------------------------------------------------------------------------
# STOCK_RADAR_SOURCE = ths | eastmoney
#   ths       （默认）K 线 + 实时行情走同花顺；全市场列表仍走东财
#   eastmoney 全部走东财（原行为）
#
# 为什么全市场列表必须留在东财：
#   同花顺排行页（q.10jqka.com.cn）有反爬，仅第 1 页可取 20 只，第 2 页起
#   返回 HTTP 401/403（需 hexin-v 动态 token，由 chameleon.js 生成）；
#   data.10jqka.com.cn 数据中心接口已全部下线（404）。
#   东财 clist 是当前唯一能一次拿全 5900+ 只股票及其涨跌幅/换手/量比/PE/市值的
#   免 token 接口，因此「全市场扫描层」保留东财，「K 线 + 个股实时行情层」切换同花顺。
DATA_SOURCE = os.environ.get("STOCK_RADAR_SOURCE", "ths").lower()
SNAPSHOT_SOURCE = "eastmoney"      # 全市场列表固定东财，见上说明
KLINE_SOURCE = os.environ.get("STOCK_RADAR_KLINE_SOURCE", DATA_SOURCE).lower()
QUOTE_SOURCE = os.environ.get("STOCK_RADAR_QUOTE_SOURCE", DATA_SOURCE).lower()

# ---------------------------------------------------------------------------
# 数据源（同花顺，无需 API Key / token）
# ---------------------------------------------------------------------------
THS_HTTP_RETRY = 3          # 同花顺单请求重试（反爬 401/403 不重试）
THS_KLINE_WORKERS = int(os.environ.get("STOCK_RADAR_THS_WORKERS", "24"))  # 同花顺 K 线并发
THS_KLINE_URL = "http://d.10jqka.com.cn/v6/line/{sym}/01/{year}.js"
THS_ALL_KLINE_URL = "http://d.10jqka.com.cn/v6/line/{sym}/01/all.js"
THS_QUOTE_URL = "http://d.10jqka.com.cn/v6/realhead/{sym}/last.js"
THS_TIME_URL = "http://d.10jqka.com.cn/v6/time/{sym}/last.js"
THS_RANK_URL = ("http://q.10jqka.com.cn/index/index/board/all/field/{field}"
                "/order/{order}/page/{page}/ajax/1/")

# ---------------------------------------------------------------------------
# 数据源（东方财富公开接口，无需 API Key）
# ---------------------------------------------------------------------------
CLIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"
SUGGEST_URL = "https://searchapi.eastmoney.com/api/suggest/get"
SUGGEST_TOKEN = "D43BF722C8E33BDC906FB84D85E326E8"

# ---------------------------------------------------------------------------
# 行情节点池（重要）
# ---------------------------------------------------------------------------
# 实测：裸域名 push2.eastmoney.com / push2his.eastmoney.com 在部分网络下会被直接
# 重置连接（RemoteDisconnected），而带数字前缀的镜像节点可以正常访问。
# 因此这里配置多节点池，请求时轮询 + 失败自动切换，避免单点不可用导致整体拉取失败。
# 列表末尾保留裸域名，作为其它网络环境下的兜底。
HOSTS_CLIST = [
    "82.push2.eastmoney.com",
    "48.push2.eastmoney.com",
    "7.push2.eastmoney.com",
    "1.push2.eastmoney.com",
    "62.push2his.eastmoney.com",
    "push2delay.eastmoney.com",
    "push2.eastmoney.com",
]
# 注意：K 线接口只能走 push2his 系域名，push2 域名只提供实时行情（clist），
# 用它请求 kline 会返回 200 + 空数据；push2delay 为延时行情，同样无 K 线。
# 实测本机网络下仅 82.push2his 稳定可用，故置于首位，其余作为备用；
# 连续失败的节点会被 data_source 自动降权剔除，网络环境变化时可自愈。
HOSTS_KLINE = [
    "82.push2his.eastmoney.com",
    "62.push2his.eastmoney.com",
    "48.push2his.eastmoney.com",
    "23.push2his.eastmoney.com",
    "7.push2his.eastmoney.com",
    "push2his.eastmoney.com",
]

# 沪深京 A 股全市场
MARKET_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"

# 快照字段：代码/市场/名称/最新价/涨跌幅/成交额/换手率/市盈率(动)/量比/
#           总市值/流通市值/市净率/60日涨跌幅/年初至今/5日涨跌幅/上市日期/行业/主力净流入
SNAPSHOT_FIELDS = (
    "f12,f13,f14,f2,f3,f6,f8,f9,f10,f20,f21,f23,f24,f25,f109,f26,f100,f62"
)

PAGE_SIZE = 100                 # 分页大小（东财 clist 单页上限 100）
PAGE_WORKERS = 6                # 快照分页并发数
KLINE_WORKERS = int(os.environ.get("STOCK_RADAR_KLINE_WORKERS", "16"))  # K 线并发数（建议 16-32）
HTTP_TIMEOUT = 15               # 单次 HTTP 超时（秒）
HTTP_RETRY = 6                  # 单个请求最大重试次数（指数退避，约 10-14s 重试窗口）
# 说明：本机若开启了系统级代理（如 127.0.0.1:7897）但代理未运行，requests 会因读取
# 系统代理而抛 ProxyError。data_source 中的 Session 已设 trust_env=False 强制直连，
# 直连全部失败时再自动回退到系统代理，双向兜底。

# ---------------------------------------------------------------------------
# 关于「候选池」的工程取舍说明
# ---------------------------------------------------------------------------
# 东财接口不提供「全市场 7 日 / 1 月区间涨跌幅」的批量字段，区间涨跌幅必须由
# 日 K 线逐只计算。对全市场 5900+ 只股票逐只拉 K 线耗时过长且易触发限流，
# 因此这里采用两步法：
#   1) 全市场快照（5900+ 只）用于搜索、估值、成交额、5 日涨跌幅等轻量因子；
#   2) 按成交额降序取前 POOL_SIZE 只作为候选池，逐只拉日 K 线计算
#      近 7 日 / 近 1 月涨跌幅、MA5/10/20、波动率与最大回撤，再综合评分。
# POOL_SIZE 可调（环境变量 STOCK_RADAR_POOL），调大可覆盖更多标的，代价是刷新更慢。
POOL_SIZE = int(os.environ.get("STOCK_RADAR_POOL", "800"))

KLINE_LOOKBACK_DAYS = 180       # K 线回溯自然日（保证 >= 30 个交易日用于 1 月区间）
# 区间口径（按交易日回溯，与需求约定一致）：
#   「最近 7 日」 ≈ 1 个自然周 ≈ 5 个交易日  -> 取收盘价相隔 5 个交易日
#   「最近 1 月」 ≈ 21 个交易日
TRADING_DAYS_1W = 5             # 「最近 7 日」= 5 个交易日
TRADING_DAYS_1M = 21            # 「最近 1 月」= 21 个交易日

# ---------------------------------------------------------------------------
# 评分模型
# ---------------------------------------------------------------------------
# 因子权重（总和必须为 1.0，可在前端/接口动态覆盖）
SCORE_WEIGHTS = {
    "momentum_7d": 0.20,        # 近 7 日涨跌幅
    "momentum_1m": 0.25,        # 近 1 月涨跌幅
    "activity": 0.15,           # 量比 / 换手率活跃度
    "trend": 0.20,              # MA5/MA10/MA20 均线多头排列
    "risk": 0.10,               # 区间波动率与最大回撤（越低越好）
    "valuation": 0.10,          # PE / PB 估值合理性
}

# 推荐榜单规模
TOP_N = 10
# 涨跌幅榜返回条数上限
RANK_LIMIT = 100

# ---------------------------------------------------------------------------
# 自动刷新
# ---------------------------------------------------------------------------
# 缓存超过该小时数视为过期（启动时若过期则后台自动刷新）
CACHE_MAX_AGE_HOURS = int(os.environ.get("STOCK_RADAR_CACHE_HOURS", "6"))
# 每个交易日定时刷新时间（HH:MM，24 小时制），调度线程每分钟检查一次
AUTO_REFRESH_TIME = os.environ.get("STOCK_RADAR_REFRESH_TIME", "15:30")
# 是否启用内置调度线程
ENABLE_SCHEDULER = os.environ.get("STOCK_RADAR_SCHEDULER", "1") == "1"

# ---------------------------------------------------------------------------
# Web 服务
# ---------------------------------------------------------------------------
HOST = os.environ.get("STOCK_RADAR_HOST", "127.0.0.1")
PORT = int(os.environ.get("STOCK_RADAR_PORT", "8848"))
DEBUG = False
