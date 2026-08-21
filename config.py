"""Configuration constants for IBKR Trader."""

import os

# ── IBKR Connection ──────────────────────────────────────────────────
IBKR_HOST = "127.0.0.1"
IBKR_PAPER_PORT = 7497
IBKR_LIVE_PORT = 7496
IBKR_GW_LIVE_PORT = int(os.environ.get("IBKR_GW_LIVE_PORT", "4001"))
IBKR_GW_PAPER_PORT = int(os.environ.get("IBKR_GW_PAPER_PORT", "4002"))
# 以上为 IB Gateway 出厂默认端口。若本机 Gateway/IBC 使用了自定义端口,
# 请通过环境变量覆盖,避免把机器专用配置写进源码。
IBKR_CLIENT_ID = 10        # Options GUI (avoid collision with other API clients)
IBKR_STOCK_CLIENT_ID = 11  # Stock trader client (stock_trader.py)
IBKR_COMBO_CLIENT_ID = 12  # Combo analyzer (combo_analyzer.py)
IBKR_MACRO_CLIENT_ID = 13  # Macro monitor (macro_monitor.py): 美债利率/原油/金银, 只读行情

# ── 新版本开关 (Gateway + 更轻的行情订阅) ────────────────────────────
# 设环境变量 IBKR_USE_GATEWAY=1 → 连 IB Gateway (端口 4001/4002) 而非 TWS
# (7496/7497), 并收紧行情订阅占用, 让期权 GUI + 正股 GUI 同开时不超过
# ~100 行账户上限。旧启动脚本 (start.bat) 不设此变量 → 行为与之前完全一致。
# 新启动脚本 start_gateway.bat 通过独立入口 main_gw.py 设置该变量。
USE_GATEWAY = os.environ.get("IBKR_USE_GATEWAY", "0") == "1"

# ── Market Data ──────────────────────────────────────────────────────
MARKET_DATA_TYPE = 1  # 1=Live, 2=Frozen, 3=Delayed, 4=Delayed-Frozen
# 单组件 (option_chain 每个到期日 tab) 同时订阅的行情上限。
# 新版 (Gateway) 调小, 让期权+正股两个 GUI 合计 <100 行账户上限。
MAX_SIMULTANEOUS_STREAMS = 45 if USE_GATEWAY else 95  # IBKR limit ~100
# 期权链显示/订阅的 ATM 上下行权价档数 (±N); 新版调小减少行情占用。
CHAIN_STRIKES_AROUND_ATM = 10 if USE_GATEWAY else 15

# ── Price Ladder ─────────────────────────────────────────────────────
# Penny Pilot (SPY, QQQ, IWM, AAPL, TSLA, NVDA, AMZN, etc.)
TICK_SIZE_SMALL = 0.01   # For options priced < $3
TICK_SIZE_LARGE = 0.05   # For options priced >= $3
TICK_THRESHOLD = 3.0     # Price threshold for tick size switch
LADDER_ROWS = 201        # Price levels (±100 from center; $2.00 at $0.01 tick)
LADDER_ROW_HEIGHT = 26   # 每个价格档行高 (须与 PriceLadderRow 固定高度一致)
LADDER_EXTEND_CHUNK = 40 # 滚轮滚到顶/底边缘时, 一次向该方向追加的档位数
LADDER_MAX_ROWS = 1600   # 点价梯最多档位数 (防止反复滚动无限扩展占内存)

# 按标的覆盖 (指数期权更粗; 少数最活跃 ETF 全系列 penny)
TICK_SIZE_OVERRIDES = {
    "SPX":  (0.05, 0.10),   # SPX: $0.05 < $3, $0.10 >= $3
    "XSP":  (0.01, 0.01),   # XSP(Mini-SPX): 全系列统一 $0.01 (CBOE 实测最小跳动)
    "NDX":  (0.05, 0.10),
    "RUT":  (0.05, 0.10),
    # Penny Interval Program 的例外类: SPY/QQQ/IWM **全价位统一 $0.01**,
    # 不走"≥$3 变 $0.05"那条通用规则 (交易所规则, 非估计)。原先按通用规则处理,
    # SPY 期权涨过 $3 后点价梯档位跳成 0.05, 且 _refresh 的 snap() 会把真实
    # 盘口 3.41 吸附到 3.40 —— 表现为"价格较高时跟 IBKR 手机端对不上", 且
    # 3.41 这种价位根本挂不出去。2026-08-10 修正。
    "SPY":  (0.01, 0.01),
    "QQQ":  (0.01, 0.01),
    "IWM":  (0.01, 0.01),
}

# ── Market Depth ─────────────────────────────────────────────────────
DEPTH_ROWS = 10          # Number of depth levels to request

# ── Account & Refresh ───────────────────────────────────────────────
ACCOUNT_REFRESH_MS = 3000  # 账户摘要订阅的保活间隔 (幂等, 已订阅时是空操作)
# IBKR 的 reqAccountSummary 只在**值变化或约每 3 分钟**才推一次 —— 总资产/可用资金
# 因此看起来半天不动。这里每 ACCOUNT_RESYNC_MS 主动 cancel+重订一次, 强制 IBKR 立刻
# 回一份新快照; 30 秒的 churn 远低于早先每 3 秒重订 (那会把 Gateway 的请求队列压垮)。
# 两次真值快照之间, 总资产由 reqPnL 流实时外推 (见 account_bar._recompute_live_net_liq),
# 所以肉眼看到的是**逐笔跳动**的净值, 而不是三分钟一跳。
ACCOUNT_RESYNC_MS = 30_000

# ── Paper Trading ────────────────────────────────────────────────────
PAPER_STARTING_CAPITAL = 10000.0

# ── Commission (IBKR Pro Fixed) ──────────────────────────────────────
COMMISSION_PER_CONTRACT = 0.65  # USD per contract per side (options)
COMMISSION_MIN = 1.00           # Minimum per order
STOCK_COMMISSION_PER_SHARE = 0.005  # USD per share (stocks, IBKR Pro Fixed)
STOCK_COMMISSION_MIN = 1.00         # Minimum per stock order
FUTURES_COMMISSION_PER_CONTRACT = 0.85  # USD per contract per side (≈IBKR 期货, 仅用于本地显示)
FUTURES_COMMISSION_MIN = 0.85           # Minimum per futures order

# ── 主题 (Theme) ─────────────────────────────────────────────────────
# 两套主题, 全部 COLOR_* 常量按启动时读到的主题注入模块全局 —— 15 个
# widget 文件在构建 UI 时引用这些常量, 因此**切换主题需重启生效**
# (顶栏「主题」下拉切换后程序会提示并自动重启)。
#   classic — 经典深蓝 (原版配色, 逐值保留)
#   scifi   — 简约科幻: 近黑深蓝底 + 电光青 accent + 霓虹绿/红,
#             文字提亮对比更清晰, 直角边框, Bahnschrift 尖锐字体
#   light   — 亮色: 白底/浅灰蓝面板 + 深藏青文字, 绿/红加深保证白底可读
#             (K 线图表仍走独立的 CHART_COLOR_* 深色常量, 不随主题变)
THEME_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "theme.json")

THEMES = {
    "classic": {
        "COLOR_BG": "#1a1a2e",
        "COLOR_BG_DARK": "#16213e",
        "COLOR_BG_PANEL": "#0f3460",
        "COLOR_TEXT": "#e0e0e0",
        "COLOR_TEXT_DIM": "#888888",
        "COLOR_GREEN": "#00c853",
        "COLOR_RED": "#ff1744",
        "COLOR_BUY": "#00c853",
        "COLOR_SELL": "#ff1744",
        "COLOR_BID_HIGHLIGHT": "#004d40",   # Teal for bid level
        "COLOR_ASK_HIGHLIGHT": "#4a4000",   # Dark yellow for ask level
        "COLOR_ATM_HIGHLIGHT": "#1a237e",   # Deep blue for ATM strike
        "COLOR_ACCENT": "#00bcd4",
        "COLOR_BORDER": "#333355",
        "COLOR_BUTTON_DISABLED": "#404040",
        "COLOR_PROFIT": "#00c853",
        "COLOR_LOSS": "#ff1744",
        "COLOR_DEPTH_BID": "#1a472a",       # Green tint for bid depth bars
        "COLOR_DEPTH_ASK": "#4a1a1a",       # Red tint for ask depth bars
        "COLOR_MY_ORDER": "#ffab00",        # Amber for my orders at price level
        # 以下为原先散落在 widget 里的硬编码色, 收编为主题键 (值不变):
        "COLOR_ACCENT_HOVER": "#0097a7",    # accent 按钮 hover
        "COLOR_BID_PRICE_BG": "#003a3a",    # 点价梯最优买价块 底/字
        "COLOR_BID_PRICE_FG": "#00e5ff",
        "COLOR_ASK_PRICE_BG": "#3a2a00",    # 点价梯最优卖价块 底/字
        "COLOR_ASK_PRICE_FG": "#ffff00",
        "COLOR_DEPTH_BID_ROW": "#1a3a2a",   # 深度条行高亮底色
        "COLOR_DEPTH_ASK_ROW": "#3a1a1a",
        "COLOR_DEPTH_BID_BAR_HL": "#2a8a4a",  # 深度条高亮填充
        "COLOR_DEPTH_ASK_BAR_HL": "#8a2a2a",
        "COLOR_BID_TEXT_HL": "#00ff88",     # 深度量文字高亮
        "COLOR_ASK_TEXT_HL": "#ff6666",
        "COLOR_ALERT_ROW": "#5d4037",       # 自选到价警报行闪烁底色
        "FONT_FAMILY": "Segoe UI",
        "FONT_SIZE": 10,
        "UI_RADIUS": "3px",
    },
    "scifi": {
        "COLOR_BG": "#060a12",              # 近黑深蓝
        "COLOR_BG_DARK": "#0a1020",
        "COLOR_BG_PANEL": "#0e2138",
        "COLOR_TEXT": "#e6f1ff",            # 蓝白, 对比更高
        "COLOR_TEXT_DIM": "#6f86a8",        # 暗字仍保持可读
        "COLOR_GREEN": "#00e696",           # 霓虹薄荷绿
        "COLOR_RED": "#ff3b5c",
        "COLOR_BUY": "#00e696",
        "COLOR_SELL": "#ff3b5c",
        "COLOR_BID_HIGHLIGHT": "#003a30",
        "COLOR_ASK_HIGHLIGHT": "#3a3300",
        "COLOR_ATM_HIGHLIGHT": "#0e2f66",
        "COLOR_ACCENT": "#00e5ff",          # 电光青
        "COLOR_BORDER": "#1b3350",          # 钢青色细边
        "COLOR_BUTTON_DISABLED": "#26314a",
        "COLOR_PROFIT": "#00e696",
        "COLOR_LOSS": "#ff3b5c",
        "COLOR_DEPTH_BID": "#0b3d26",
        "COLOR_DEPTH_ASK": "#42121e",
        "COLOR_MY_ORDER": "#ffc400",
        "COLOR_ACCENT_HOVER": "#5eefff",    # 电光青 hover 提亮
        "COLOR_BID_PRICE_BG": "#003a3a",
        "COLOR_BID_PRICE_FG": "#00e5ff",
        "COLOR_ASK_PRICE_BG": "#3a2a00",
        "COLOR_ASK_PRICE_FG": "#ffff00",
        "COLOR_DEPTH_BID_ROW": "#1a3a2a",
        "COLOR_DEPTH_ASK_ROW": "#3a1a1a",
        "COLOR_DEPTH_BID_BAR_HL": "#2a8a4a",
        "COLOR_DEPTH_ASK_BAR_HL": "#8a2a2a",
        "COLOR_BID_TEXT_HL": "#00ff88",
        "COLOR_ASK_TEXT_HL": "#ff6666",
        "COLOR_ALERT_ROW": "#5d4037",
        "FONT_FAMILY": "Bahnschrift",       # Windows 内置 DIN 风格, 尖锐工业感
        "FONT_SIZE": 10,
        "UI_RADIUS": "0px",                 # 直角 — 简约科幻
    },
    "light": {
        "COLOR_BG": "#f4f6fb",              # 浅灰蓝页面底
        "COLOR_BG_DARK": "#ffffff",         # 表格/输入框 — 纯白
        "COLOR_BG_PANEL": "#e4eaf4",        # 表头/选中底
        "COLOR_TEXT": "#1c2433",            # 深藏青, 白底高对比
        "COLOR_TEXT_DIM": "#68748c",
        "COLOR_GREEN": "#008f45",           # 加深的绿/红 — 白底可读,
        "COLOR_RED": "#d61f3d",             # 做按钮底时白字也够对比
        "COLOR_BUY": "#008f45",
        "COLOR_SELL": "#d61f3d",
        "COLOR_BID_HIGHLIGHT": "#cdeee2",   # 浅色行高亮 (配深字)
        "COLOR_ASK_HIGHLIGHT": "#f7eec6",
        "COLOR_ATM_HIGHLIGHT": "#d8e4fb",
        "COLOR_ACCENT": "#0077c2",
        "COLOR_BORDER": "#c3cddd",
        "COLOR_BUTTON_DISABLED": "#c9cfda",
        "COLOR_PROFIT": "#008f45",
        "COLOR_LOSS": "#d61f3d",
        "COLOR_DEPTH_BID": "#bce5cb",       # 深度条填充 — 白底上可见的浅绿/粉
        "COLOR_DEPTH_ASK": "#f4c6cd",
        "COLOR_MY_ORDER": "#cc8400",        # 琥珀加深, 白底可读
        "COLOR_ACCENT_HOVER": "#005e99",
        "COLOR_BID_PRICE_BG": "#c8f0ee",
        "COLOR_BID_PRICE_FG": "#006b80",
        "COLOR_ASK_PRICE_BG": "#f7edc0",
        "COLOR_ASK_PRICE_FG": "#8a6d00",
        "COLOR_DEPTH_BID_ROW": "#dff3e6",
        "COLOR_DEPTH_ASK_ROW": "#fbe3e7",
        "COLOR_DEPTH_BID_BAR_HL": "#6fcd92",
        "COLOR_DEPTH_ASK_BAR_HL": "#e8909e",
        "COLOR_BID_TEXT_HL": "#00602e",
        "COLOR_ASK_TEXT_HL": "#a01226",
        "COLOR_ALERT_ROW": "#ffd9a8",       # 浅橙闪烁, 配深字
        "FONT_FAMILY": "Segoe UI",
        "FONT_SIZE": 10,
        "UI_RADIUS": "3px",
    },
}


def load_theme_name() -> str:
    """读取已保存的主题名; 文件缺失/损坏/未知主题一律回退 classic。"""
    try:
        import json
        with open(THEME_FILE, "r", encoding="utf-8") as f:
            name = json.load(f).get("theme", "classic")
        return name if name in THEMES else "classic"
    except Exception:
        return "classic"


def save_theme_name(name: str) -> None:
    """保存主题选择 (重启后 load_theme_name 读到并生效)。"""
    import json
    with open(THEME_FILE, "w", encoding="utf-8") as f:
        json.dump({"theme": name}, f, ensure_ascii=False, indent=2)


THEME_NAME = load_theme_name()
# 把选中主题的所有键注入为模块级常量 (COLOR_* / FONT_* / UI_RADIUS),
# 下游 `from config import COLOR_GREEN` 等写法全部不用改。
globals().update(THEMES[THEME_NAME])

# ── Forex ────────────────────────────────────────────────────────────
FOREX_PAIRS = [
    ("USD", "HKD"),
    ("USD", "CNH"),
    ("USD", "EUR"),
    ("USD", "GBP"),
    ("USD", "JPY"),
]

# ── Ignored IBKR Error Codes ────────────────────────────────────────
# Only truly harmless informational codes.
# Data-connection codes (2100, 2103-2108) are handled specially in
# IBKRApp.error() — they get logged and surfaced to the GUI.
# 10167 is also handled separately (one-time delayed-data warning).
IGNORED_ERROR_CODES = {
    2119,                          # Market data farm connection restored (info)
    2150, 2157, 2158, 2168, 2169,  # Account / permission info
    10090, 10089, 10168,           # Market data subscription info
    2176,  # Fractional share size trimmed (ibapi 9.81 < server v163) —
           # harmless: only fractional volume decimals are dropped
}

# Data-connection error codes — surfaced as warnings, not silenced
DATA_CONNECTION_ERROR_CODES = {
    2100,  # API client has been unsubscribed from account data
    2103,  # Market data farm connection is broken
    2104,  # Market data farm connection is OK (recovery)
    2105,  # HMDS data farm connection is broken
    2106,  # HMDS data farm connection is OK (recovery)
    2107,  # HMDS data farm connection is inactive
    2108,  # Market data farm connection is inactive
}

# ── Option Pricing (Black-Scholes 理论价计算器) ──────────────────────
RISK_FREE_RATE = 0.045        # 无风险年利率 (≈美国短债); 计算器默认值, 可在界面调整
DIVIDEND_YIELD = 0.0          # 标的连续股息率 (SPY/SPX 用 0 即可)
OPTION_MARKET_CLOSE_ET = 16   # 期权到期日收盘小时 (ET); 用于计算剩余时间 T
CALCULATOR_REFRESH_MS = 300   # 计算器随实时行情/时间衰减刷新间隔 (越小越实时)

# ── Index Symbols (secType=IND, not STK) ────────────────────────────
# 含 CBOE 美债收益率指数 (IRX 13周 / FVX 5年 / TNX 10年 / TYX 30年): 计算器右下角
# 利率行用 FVX/TNX 订阅实时收益率。指数值口径: TNX/FVX/TYX = 收益率×10 (显示需 ×0.1),
# IRX ≈ 收益率 (×1.0) —— 换算在 OptionCalculator._RATE_SYMBOLS 里按 scale 处理。
# 注: 2 年期短端已改走 Yahoo `2YY=F` (延迟), 不再用 IRX; IRX/TYX 仍留作 IND 类型注册。
INDEX_SYMBOLS = {"SPX", "XSP", "NDX", "RUT", "VIX", "DJX",
                 "IRX", "FVX", "TNX", "TYX"}

# ── Futures (常用合约: 交易所 + 乘数 + 最小跳动 + 名称) ─────────────────
# 用于点价交易程序的「期货」模式。合约月份由 reqContractDetails 自动解析
# (近月 + 之后几个季月), tick/交易所/乘数从这里取。
# 格式: 根代码 -> (exchange, multiplier, tick_size, 描述)
FUTURES_SPECS = {
    # 股指期货 (季度合约: 3/6/9/12)
    "ES":  ("CME",   50,   0.25,  "E-mini S&P 500"),
    "MES": ("CME",   5,    0.25,  "Micro E-mini S&P 500"),
    "NQ":  ("CME",   20,   0.25,  "E-mini Nasdaq 100"),
    "MNQ": ("CME",   2,    0.25,  "Micro E-mini Nasdaq 100"),
    "RTY": ("CME",   50,   0.10,  "E-mini Russell 2000"),
    "M2K": ("CME",   5,    0.10,  "Micro E-mini Russell 2000"),
    "YM":  ("CBOT",  5,    1.0,   "E-mini Dow"),
    "MYM": ("CBOT",  0.5,  1.0,   "Micro E-mini Dow"),
    # 能源 / 金属 (月度合约)
    "CL":  ("NYMEX", 1000, 0.01,  "Crude Oil WTI"),
    "MCL": ("NYMEX", 100,  0.01,  "Micro Crude Oil"),
    "GC":  ("COMEX", 100,  0.10,  "Gold"),
    "MGC": ("COMEX", 10,   0.10,  "Micro Gold"),
    # 注: 点价梯按 2 位小数网格, 故只收录 tick ≥ 0.01 的品种
    # (如 SI 银 tick=0.005 暂不收录, 以免价格网格错位)。
}
FUTURES_SYMBOLS = list(FUTURES_SPECS.keys())
# 期货模式下合约月份下拉显示的最多档数 (近月起算, 含 ~3 个月后的季月)
FUTURES_MAX_EXPIRIES = 5

# 期货开多(BUY)强制带止盈+止损: 未在「条件单」面板设置好二者就拦截下单,
# 通过后下单并自动挂上 (市价单立即挂; 限价单等成交回报后再挂)。设 False 关闭强制。
FUTURES_REQUIRE_BRACKET = True

# ── Default Symbols ──────────────────────────────────────────────────
DEFAULT_SYMBOLS = ["SPY", "SPX", "QQQ", "IWM", "AAPL", "TSLA", "NVDA", "AMZN", "META"]

# ── Option Chain ─────────────────────────────────────────────────────
MAX_EXPIRY_TABS_PER_RANGE = 10  # Show at most 10 expiries per range filter

# ── SPX Options Trading Sessions (all times ET) ───────────────────
# GTH = Global Trading Hours (夜盘/盘前): 20:15 → 09:15 next day
# RTH = Regular Trading Hours (正常盘): 09:30 → 16:15
# Curb = After-hours (盘后): 16:15 → 17:00 (limited)
# Note: SPY options are RTH only; SPX/SPXW support GTH+RTH
SPX_SESSION_GTH_START = (20, 15)  # 8:15 PM ET
SPX_SESSION_GTH_END = (9, 15)    # 9:15 AM ET
SPX_SESSION_RTH_START = (9, 30)   # 9:30 AM ET
SPX_SESSION_RTH_END = (16, 15)    # 4:15 PM ET

# Symbols that support extended hours (GTH) trading
EXTENDED_HOURS_SYMBOLS = {"SPX"}

# ── Chart (K-Line) ─────────────────────────────────────────────────
# (display_name, ibkr_bar_size, duration, keep_up_to_date)
CHART_TIMEFRAMES = {
    "1秒":   ("1 secs",  "1800 S", False),
    "5秒":   ("5 secs",  "3600 S", False),
    "15秒":  ("15 secs", "7200 S", False),
    "30秒":  ("30 secs", "14400 S", False),
    "1分钟": ("1 min",   "1 D",    True),
    "5分钟": ("5 mins",  "1 W",    True),
    "15分钟":("15 mins", "2 W",    True),
    "30分钟":("30 mins", "1 M",    True),
    "1小时": ("1 hour",  "1 M",    True),
    "2小时": ("2 hours", "1 M",    True),
    "4小时": ("4 hours", "1 M",    True),
    "日线":  ("1 day",   "1 Y",    True),
    "周线":  ("1 week",  "5 Y",    False),
    "月线":  ("1 month", "10 Y",   False),
}

CHART_COLOR_CANDLE_UP = "#00c853"
CHART_COLOR_CANDLE_DOWN = "#ff1744"
CHART_COLOR_MA5 = "#ffeb3b"
CHART_COLOR_MA20 = "#ff9800"
CHART_COLOR_MA50 = "#e040fb"
CHART_COLOR_MA200 = "#00bcd4"
CHART_COLOR_VWAP = "#ffffff"
CHART_COLOR_VOLUME_UP = "#1b5e20"
CHART_COLOR_VOLUME_DOWN = "#b71c1c"
CHART_COLOR_BG = "#0d0d1a"
CHART_COLOR_CROSSHAIR = "#888888"
