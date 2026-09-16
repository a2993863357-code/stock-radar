---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 49e5347e1cffaf748aa84f79ff883146_c2c0e593b00f11f1b128525400f8a581
    ReservedCode1: dbnNYI/4zRVcIZMGUK1v+vpCTtao4YkmW2P8NzVv//05C9Vbk94e1YcL84JKCNL9eDrU2AODvnyO13Bb4wD9pYYe1Yv47c25ahvABJxOmEQOuToAjlMr5BteidRuIDuA5LUV7pnKq8J5/XRlREV8t0pVeRGIwisrVXd6ny1bwT9k7B20FTsOnqa6xPc=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 49e5347e1cffaf748aa84f79ff883146_c2c0e593b00f11f1b128525400f8a581
    ReservedCode2: dbnNYI/4zRVcIZMGUK1v+vpCTtao4YkmW2P8NzVv//05C9Vbk94e1YcL84JKCNL9eDrU2AODvnyO13Bb4wD9pYYe1Yv47c25ahvABJxOmEQOuToAjlMr5BteidRuIDuA5LUV7pnKq8J5/XRlREV8t0pVeRGIwisrVXd6ny1bwT9k7B20FTsOnqa6xPc=
---

# Stock Radar · 本地 A 股行情与选股助手

一个跑在本机的轻量股票行情分析工具：每天自动抓取沪深京 A 股全市场行情，计算 **最近 7 日 / 最近 1 个月** 的涨跌幅，按多因子模型给出 **推荐购买指数**，首页展示 Top10 榜单；也可以直接输入股票名称或代码查询个股详情。

数据全部来自东方财富公开行情接口，**无需任何 API Key**，数据落地本机 JSON 缓存，断网也能查看上一次结果。

---

## 一、功能一览

| 功能 | 说明 |
| --- | --- |
| 推荐购买指数 Top10 | 对候选池股票按六大因子综合打分（0-100），首页展示前 10 名，含评分明细与推荐理由 |
| 涨幅榜 / 跌幅榜 | 全市场按区间涨跌幅排序，可切换「最近 7 日」「最近 1 个月」 |
| 周期切换 | 最近 7 日（5 个交易日）、最近 1 个月（21 个交易日），按交易日回溯，口径统一 |
| 个股搜索 | 输入股票名称、代码或拼音首字母即可查询，返回实时行情、区间涨跌幅、均线、评分与因子明细 |
| 自动刷新 | 应用启动时若缓存过期自动后台刷新；内置调度线程在交易日 15:30 自动刷新 |
| 手动刷新 | 页面提供刷新按钮，调用 `POST /api/refresh` 立即重抓数据 |
| 本地缓存 | 全市场快照 + 逐只日 K 线落地 `data/` 目录，避免重复请求、规避接口限流 |

---

## 二、目录结构

```
stock-radar/
├── app.py                 # Flask 入口：页面路由 + REST API + 调度线程
├── config.py              # 全局配置：节点池、并发、评分权重、刷新策略
├── data_source.py         # 东方财富接口封装：快照、日 K 线、搜索、节点轮询与重试
├── scoring.py             # 多因子评分模型
├── install_task.ps1       # 注册 Windows 计划任务（无人值守每日刷新）
├── run.bat                # 一键启动脚本（自动拉起服务并打开浏览器）
├── requirements.txt       # 依赖清单
├── README.md
├── templates/
│   └── index.html         # 首页模板
├── static/
│   ├── css/style.css
│   └── js/app.js
└── data/                  # 数据缓存（自动生成）
    ├── snapshot.json      # 全市场快照
    ├── scores.json        # 评分结果（榜单数据源）
    ├── meta.json          # 刷新元信息
    ├── refresh.log        # 刷新日志
    └── kline_cache/       # 逐只日 K 线缓存
```

---

## 三、环境要求

- Windows 10 / 11
- Python 3.12+
- 依赖：`flask`、`requests`、`pandas`（见 `requirements.txt`）

安装依赖：

```bat
python -m pip install -r requirements.txt
```

---

## 四、启动方式

**方式一：一键启动（推荐）**

双击项目根目录下的 `run.bat`，脚本会启动服务并自动打开浏览器。

**方式二：手动启动**

```bat
cd /d "项目路径\stock-radar"
python app.py
```

启动后访问：**http://127.0.0.1:8848**

**方式三：仅刷新数据（不启服务）**

```bat
python app.py --refresh
```

> 端口被占用时，可设置环境变量 `STOCK_RADAR_PORT` 更换端口。

---

## 五、数据来源

全部使用东方财富公开行情接口：

| 用途 | 接口 |
| --- | --- |
| 全市场快照 | `push2.eastmoney.com/api/qt/clist/get`（沪深京 A 股，分页拉全量） |
| 日 K 线 | `push2his.eastmoney.com/api/qt/stock/kline/get`（日线、前复权） |
| 个股实时 | `push2.eastmoney.com/api/qt/stock/get` |
| 股票检索 | `searchapi.eastmoney.com/api/suggest/get` |

**节点池与限流说明**：裸域名在部分网络下会被重置连接，因此 `config.py` 中配置了多节点池（`82/48/7/1.push2` 与 `82/62/48/23/7.push2his`），请求时轮询并在失败时自动切换；连续失败的节点会被临时降权剔除，网络恢复后自愈。短时间内高频请求可能触发接口端限流（表现为连接被重置或返回空数据），稍等几分钟再刷新即可。

---

## 六、评分模型

推荐购买指数 = 六大因子加权得分（0-100），权重可在 `config.py` 的 `SCORE_WEIGHTS` 中调整。

| 因子 | 权重 | 含义 |
| --- | --- | --- |
| `momentum_7d` | 20% | 最近 7 日（5 个交易日）涨跌幅，衡量短期动量 |
| `momentum_1m` | 25% | 最近 1 个月（21 个交易日）涨跌幅，衡量中期趋势 |
| `activity` | 15% | 量比 / 换手率，衡量资金活跃度 |
| `trend` | 20% | MA5 / MA10 / MA20 均线多头排列程度 |
| `risk` | 10% | 区间波动率与最大回撤，越低得分越高 |
| `valuation` | 10% | 市盈率 / 市净率估值合理性 |

得分区间参考：**80 分以上** 各因子共振、形态较强；**60-80 分** 多数因子向好；**60 分以下** 存在明显短板。榜单同时给出 `reasons.pros` / `reasons.cons`，说明加分与减分理由。

> 候选池机制：东财接口不提供全市场区间涨跌幅的批量字段，必须逐只由日 K 线计算。为避免对 5900+ 只股票逐只拉取导致耗时过长与限流，程序按成交额降序取前 `POOL_SIZE`（默认 800）只作为候选池做精细化评分，全市场快照则覆盖全部股票用于搜索与基础行情。可通过环境变量 `STOCK_RADAR_POOL` 调整候选池大小。

---

## 七、缓存与自动刷新

- 缓存过期时长：`CACHE_MAX_AGE_HOURS` 默认 **6 小时**，应用启动时若快照或评分超过该时长，会在后台线程自动刷新。
- 定时刷新：内置调度线程每个交易日 **15:30**（`AUTO_REFRESH_TIME`）自动刷新一次；可通过 `STOCK_RADAR_SCHEDULER=0` 关闭。
- 无人值守：执行 `install_task.ps1` 可注册 Windows 计划任务，即使不打开网页也会每天自动刷新数据。

常用环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `STOCK_RADAR_PORT` | 8848 | 服务端口 |
| `STOCK_RADAR_POOL` | 800 | 候选池股票数量 |
| `STOCK_RADAR_KLINE_WORKERS` | 16 | K 线并发线程数 |
| `STOCK_RADAR_CACHE_HOURS` | 6 | 缓存过期小时数 |
| `STOCK_RADAR_REFRESH_TIME` | 15:30 | 每日自动刷新时间 |
| `STOCK_RADAR_SCHEDULER` | 1 | 是否启用内置调度线程 |

---

## 八、常见问题

**1. 榜单为空或报「接口不可用」**
多为接口端限流或网络波动。等待 2-3 分钟后点击页面刷新按钮重试；若持续失败，检查是否能正常访问 `quote.eastmoney.com`。

**2. 区间涨跌幅显示为空**
说明该股票日 K 线未取到。可删除 `data/kline_cache/` 下对应文件后重新刷新，或直接整体刷新。

**3. 刷新很慢**
候选池越大越慢。K 线阶段为逐只请求，正常情况下 800 只约 1-2 分钟；若明显变慢，通常是节点被限流导致重试增加，稍后重试即可。

**4. 服务端口被占用**
设置环境变量后重启：`set STOCK_RADAR_PORT=8849` 再运行 `app.py`。

---

## 九、免责声明

本工具所有数据来自第三方公开接口，仅用于本地学习、研究与行情观察，**不构成任何投资建议**。评分模型为规则化量化打分，不预测股价，不保证收益。据此操作，风险自负。
*（内容由AI生成，仅供参考）*
