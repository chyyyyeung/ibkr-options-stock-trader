# IBKR 点价交易 (ibkr_trader)

基于 **PyQt5 + ibapi** 的 IBKR 点价交易桌面程序。主程序 **期权点价 GUI**(`main.py`,clientId=10)
顶栏「类型」三选一即可在**期权 / 正股 / 期货**间切换交易;另有独立的**期权组合分析器**
(`combo_analyzer.py`,clientId=12)。核心特性:Futu 风格点价梯(深度摆盘 + 点击下单)、
期权 T 型报价链、多腿组合策略、K 线图、实时持仓与每仓位今日盈亏、真实/模拟双引擎切换。

**当前公开版本: `v1.2.0` (2026-08-21)。**

> **📌 维护约定(必读)**:本文件是 `ibkr_trader/` 的**唯一总体文档**,作为活文档维护。
> **每次改动本目录的代码(新增/删除文件、改架构、改配置、修 bug、调行为)后,必须同步更新本文件**:
> 改了结构就更新对应小节(目录树 / 文件详解 / 配置速查 / 线程表),
> 并在文末 **[变更记录](#8-变更记录-changelog)** 追加一行(日期 + 一句话说明 + 涉及文件)。
> 文档与代码不一致时,以代码为准并立即修文档。

---

## 1. 快速开始

| 程序 | 启动方式 | clientId | 入口 | 桌面快捷方式 |
|------|---------|----------|------|------------|
| 期权点价 GUI (Gateway, 新版) | `start_gateway.bat` (`pythonw main_gw.py`) | 10 | `main_gw.py` | **"IBKR 点价交易"** |
| 期权点价 GUI (TWS, 旧版) | `start.bat` (`pythonw main.py`) | 10 | `main.py` | — |
| 期权组合分析器 (Gateway, 新版) | `start_combo_gateway.bat` (`pythonw combo_analyzer_gw.py`) | 12 | `combo_analyzer_gw.py` | — |
| 期权组合分析器 (TWS, 旧版) | `start_combo.bat` (`pythonw combo_analyzer.py`) | 12 | `combo_analyzer.py` | — |
| 宏观行情监控 (Gateway) | `start_macro_gateway.bat` (设 `IBKR_USE_GATEWAY=1` + `pythonw macro_monitor.py`) | 13 | `macro_monitor.py` | — |
| 宏观行情监控 (TWS) | `start_macro.bat` (`pythonw macro_monitor.py`) | 13 | `macro_monitor.py` | — |

> **正股/期货交易已并入期权点价 GUI**:顶栏「类型」三选一(期权/正股/期货)即可切换,
> 复用点价梯/持仓/委托。原独立正股 client(`stock_trader.py` / `stock_trader_gw.py`)及其
> 启动 bat、桌面「IBKR 正股交易」快捷方式**均已删除**(功能完全被主 GUI 覆盖)。

> 桌面快捷方式「IBKR 点价交易」现指向 **Gateway 新版**(`main_gw.py`,需先登录 IB Gateway:
> 4001 实盘 / 4002 模拟)。旧 TWS 版仍可用 `start.bat` 手动启动;新旧/旧版文件名独立、
> 互不杀进程,可同时运行对比。

- 用 `pythonw` 启动(无控制台窗口)。`stdout`/`stderr` 自动重定向到 `logs/app_YYYY-MM-DD.log`。
- 启动时 `single_instance.kill_previous_instances()` 会**只杀掉同一脚本的旧实例**
  (期权 GUI / 组合分析器各自互不影响),从而在重连前释放 TWS 中占用的 clientId。
- 前置条件:TWS 已登录并开启 API(Global Configuration → API → Enable ActiveX and Socket Clients),
  端口 7496(live)/7497(paper)。详见文末"TWS API vs Gateway"。
- **模式三选一**(顶栏「模式」下拉):**本地模拟**(本地撮合,无需真实账户)/ **IBKR模拟盘**
  (订单真实发到 7497 的模拟账户,测试下单链路,需先登录一个 paper TWS 会话)/ **实盘**(7496,真实资金)。

---

## 2. 目录结构

```
ibkr_trader/
├── main.py                 # 期权 GUI 入口 — TWS 旧版 (clientId=10, 日志重定向, 杀旧实例, 启动 QApplication)
├── main_gw.py              # 期权 GUI 入口 — Gateway 新版 (设 IBKR_USE_GATEWAY=1, 复用 MainWindow, 标题加[GW])
│   # 注: 正股交易已并入主 GUI「类型」切换, 原独立 stock_trader.py / stock_trader_gw.py 已删除
├── combo_analyzer.py       # 期权组合分析器入口 — TWS 旧版 (clientId=12, 组合历史价合成 + 组合原子交易)
├── combo_analyzer_gw.py    # 期权组合分析器入口 — Gateway 新版 (设 IBKR_USE_GATEWAY=1, 复用 ComboAnalyzerWindow, 标题加[GW])
├── start_combo.bat         # 组合分析器启动脚本 (TWS 旧版)
├── start_combo_gateway.bat # 组合分析器启动脚本 (Gateway 新版)
├── macro_monitor.py        # 宏观行情监控入口 (clientId=13, 只读): 美债利率(CBOE指数)+原油/金银(连续期货), 当前价+1/3/6/12月区间高低
├── start_macro.bat         # 宏观行情监控启动脚本 (TWS)
├── start_macro_gateway.bat # 宏观行情监控启动脚本 (Gateway, 设 IBKR_USE_GATEWAY=1)
├── combo_positions.json    # 自动生成: 组合持仓分组 (IBKR 不保留分组, 本地持久化)
├── main_window.py          # 期权 GUI 主窗口 MainWindow — 组装所有 widget + 信号连线
├── ibkr_engine.py          # IBKR API 引擎 (EWrapper/EClient + 下单/撤单 + Qt 信号桥)  ★核心
├── paper_engine.py         # 模拟交易引擎 (复用 IBKR 行情, 本地撮合成交)
├── conditional_orders.py   # 本地条件单管理器 (止盈/止损限价 + 标的价触发市价卖出, 持久化 conditional_orders.json; 触发时按 API 持仓核对数量、仓位已平/合约过期自动作废、arm 去重)
├── watchlist.py            # 自选监控管理器 (0.5s 巡检现价 + 每合约任意多条到价警报(一次性, 右键追加), 持久化 watchlist.json, 启动自动清理过期合约)
├── models.py               # 纯数据模型 (dataclass + Enum), 无 Qt/IBKR 依赖
├── config.py               # 全部常量 (连接/费率/颜色/tick/图表/交易时段)
├── single_instance.py      # 启动辅助: 杀掉同脚本的旧进程以释放 clientId
├── crash_handler.py        # 全局崩溃捕获 (未处理异常/线程异常/硬崩溃/Qt致命 → 落日志, 不静默闪退)
├── start.bat               # 期权 GUI 启动脚本 (TWS 旧版)
├── start_gateway.bat       # 期权 GUI 启动脚本 (Gateway 新版)
├── check_spx_options.py    # 独立诊断脚本: 探测 SPX 期权合约/交易时段
├── check_option_history.py # 独立诊断脚本: 检测账户是否有「期权历史数据」权限 (clientId=99)
├── app.ico / app_icon.png  # 期权 GUI 图标 (期权 GUI + 组合分析器共用)
├── logs/                   # 运行日志 + 拒单日志 (自动生成)
│   ├── app_YYYY-MM-DD.log          # 期权 GUI 控制台输出
│   ├── combo_app_YYYY-MM-DD.log    # 组合分析器控制台输出
│   ├── macro_app[_gw]_YYYY-MM-DD.log   # 宏观行情监控控制台输出
│   └── order_rejects_YYYY-MM-DD.jsonl  # 拒单详情 (每行一个 JSON)
└── widgets/
    ├── __init__.py
    ├── symbol_bar.py        # 顶栏: 代码搜索(自动补全)+连接状态+模式切换
    ├── option_chain.py      # 期权 T 型报价链 (到期日 Tab + 日期范围过滤)
    ├── price_ladder.py      # 点价梯 (5 列深度摆盘 + 点击下单 + 持仓摘要)  ★核心
    ├── position_panel.py    # 持仓面板 (期权 + 正股/ETF, P/L + 今日盈亏)
    ├── order_panel.py       # 委托面板 (挂单/历史, 撤单, 拒单标红)
    ├── account_bar.py       # 账户摘要条 (两行: 资金摘要 / 账户名+美东时钟)
    ├── currency_balance.py  # 各币种现金余额条 (正股 client 用; 期权 GUI 账户栏已不再嵌)
    ├── option_calculator.py # 期权理论价计算器 (Black-Scholes, 右下角, 随实时IV+时间刷新)
    ├── currency_dialog.py   # 外汇兑换对话框 (USD ↔ HKD/CNH/EUR/...)
    ├── quantity_selector.py # 数量选择小部件 (1–100 张)
    ├── strategy_defs.py     # 多腿策略模板 (纯数据: 牛市价差/蝶式/铁鹰/跨式...)
    ├── combo_pricing.py     # 组合定价纯逻辑 (净价/历史合成/行权价自动分配, 可单测)
    ├── strategy_window.py   # 策略构建窗口 (懒加载, 组合腿 + combo 下单)
    ├── chart_window.py      # K 线图窗口 (懒加载, numpy+pyqtgraph, 无限滚动+实时)
    ├── option_chart_window.py # 期权当日 1 分钟图 (期权链双击打开, 轻量, 10s 轮询)
    ├── candlestick_item.py  # pyqtgraph 自绘 OHLC 蜡烛 (涨空心/跌实心)
    └── chart_indicators.py  # 指标计算 (MA / VWAP / 量柱颜色)
```

---

## 3. 架构与数据流

```
       ┌─────────────────────────────────────────────────────────┐
       │                       TWS (port 7496/7497)                │
       └───────────────▲───────────────────────────┬──────────────┘
        EClient 请求    │                           │ EWrapper 回调
                        │                           ▼
       ┌────────────────┴───────────────────────────────────────┐
       │  IBKRApp(EWrapper, EClient)   —  ibkr_engine.py         │
       │  在独立 reader 线程运行 (app.run())                       │
       └───────────────────────────┬────────────────────────────┘
                                    │ emit pyqtSignal (线程安全跨线程)
                                    ▼
       ┌────────────────────────────────────────────────────────┐
       │  IBKRSignalBridge(QObject)  —  纯信号集合 (reader→GUI)   │
       └───────────────────────────┬────────────────────────────┘
                                    ▼
       ┌────────────────────────────────────────────────────────┐
       │  IBKREngine  (高层封装: connect/下单/订阅/持仓/PnL)       │
       │  ▲ 也可换成 PaperEngine (同样的 bridge 信号接口)          │
       └───────────────────────────┬────────────────────────────┘
                                    ▼  connect 信号到各 widget 的槽
       ┌────────────────────────────────────────────────────────┐
       │  MainWindow  —  组装 widgets, 把信号连到 UI 更新          │
       │   SymbolBar · OptionChain · PriceLadder · PositionPanel  │
       │   OrderPanel · AccountBar · 中央 Tab(单腿点价/多腿组合)   │
       │   + ChartWindow(懒加载)                                  │
       └────────────────────────────────────────────────────────┘
```

**关键设计:线程边界靠 Qt 信号跨越。** ibapi 的 reader 线程绝不直接碰 Qt widget;
所有回调都通过 `IBKRSignalBridge` 上的 `pyqtSignal` emit,Qt 把它排队到 GUI 线程执行槽函数。

**三种模式 (`TradingMode`)。**
- **本地模拟 (`PAPER`)** — 走 `PaperEngine`,连 7497 仅借行情,本地按 `BUY: ask<=limit / SELL: bid>=limit`
  撮合,**不向 TWS 发单**、不允许做空。无需真实模拟账户即可试用。
- **IBKR模拟盘 (`IBKR_PAPER`)** — 走真实 `IBKREngine`,连 **7497** 把订单**真实提交到 IBKR 模拟账户**,
  用于端到端验证下单链路 (placeOrder→TWS→成交回报),不涉及真实资金。需先登录一个 paper TWS 会话。
- **实盘 (`LIVE`)** — 走真实 `IBKREngine`,连 **7496**,真实资金;切换时弹确认框。

判定靠 `TradingMode.uses_ibkr_engine` (仅 PAPER 为 False) 与 `is_live_port` (仅 LIVE 连 7496)。

**双引擎可互换。** `IBKREngine` 与 `PaperEngine` 暴露相同的 bridge 信号名(见 `PaperSignalBridge`
逐一对应 `IBKRSignalBridge`),所以 UI 代码无需关心当前用的哪个引擎。

---

## 4. 文件详解

### 4.1 入口层

**`main.py`** (54 行) — 期权 GUI 入口。
- pythonw 下重定向 stdout/stderr 到 `logs/app_*.log`;有控制台时 `os.system("")` 开启 ANSI。
- `kill_previous_instances(__file__)` → 高 DPI 属性 → `QApplication` → `MainWindow().show()` → `app.exec_()`。

> 正股交易已并入主 GUI(顶栏「类型」→ 正股/期货),原独立 `stock_trader.py` / `stock_trader_gw.py`
> 已删除;`MainWindow` 通过伪合约(`right='STK'/'FUT'`)复用点价梯/持仓/委托完成正股/期货下单。

**`single_instance.py`** (61 行) — `kill_previous_instances(script_path)`:
遍历进程,匹配 `python.exe/pythonw.exe` 且命令行运行**同名脚本、同目录**的旧实例并 `terminate()`
(超时则 `kill()`)。`_runs_script()` 处理相对路径(用进程 cwd 解析)。

### 4.2 数据模型 `models.py` (233 行,纯 dataclass/Enum)

- **Enum**:`OrderAction`(BUY/SELL)、`OrderStatus`(PendingSubmit/Submitted/Filled/Cancelled/Error)、
  `TradingMode`(Paper 本地模拟 / IBKRPaper IBKR模拟盘 / Live 实盘;含 `label`/`uses_ibkr_engine`/`is_live_port`)、
  `InstrumentType`(OPT/STK/ETF)、`OrderType`(LMT/MKT)。
- **`OptionInfo`** — 单个期权合约。`display_name`(如 `SPY 260516 C 585`,正股显示 `SYM (正股)`)、
  `mid`、`to_ibkr_key()`(tick 订阅唯一键;正股共用 `__stock__SYM` 键空间)。
- **`OrderInfo`** — 委托。含 `error_msg`(拒单原因)、`display_action`/`display_status`(中文)。
- **`PositionInfo`** — 期权持仓。`unrealized_pnl`/`net_pnl`(减佣金)/`pnl_pct`/`market_value`/`cost_basis`。
- **`AccountSummary`** — 账户净值/现金/购买力/盈亏。
- **`PortfolioPosition`** — 通用持仓(期权/正股/ETF),含 `daily_pnl`(来自 `reqPnLSingle`)、
  `has_pnl_data`、`multiplier`、`instrument_type`(中文)。
- **`ComboLegInfo`** — 组合订单单腿。**`DepthRow`** — 深度摆盘一行(价/买量/卖量/我的买卖挂单量)。

### 4.3 IBKR 引擎 `ibkr_engine.py` (1580 行) ★

**`IBKRSignalBridge(QObject)`** — reader 线程 → GUI 线程的全部信号定义,例如:
`tick_updated(key,bid,ask,last)`、`chain_ready`、`order_status_changed`、`execution_received`、
`portfolio_position_received`、`pnl_single_updated`、`depth_updated`、`historical_bars_ready`/
`historical_bar_update`、`open_order_received`(重连恢复挂单)、`order_rejected`、`symbol_search_results`。

**`IBKRApp(EWrapper, EClient)`** — 实现的 ibapi 回调:
- 连接/错误:`nextValidId`、`connectionClosed`、`error`(按 `IGNORED_ERROR_CODES` /
  `DATA_CONNECTION_ERROR_CODES` 分类;10167 延迟数据一次性提示;326 = clientId 占用)。
- 合约/链:`contractDetails(End)`、`securityDefinitionOptionParameter(End)`、`symbolSamples`(代码搜索)。
- 行情:`tickPrice`/`tickSize`/`tickString`/`tickGeneric`、`tickByTickBidAsk`/`tickByTickAllLast`、
  `tickOptionComputation`(IV + Greeks + 标的价 undPrice, 写入 `_tick_data[key]` 供计算器轮询;
  `subscribe_option_tick` 对期权请求 generic tick `106` 以确保 IV 下发)。
- 历史:`historicalData`/`historicalDataEnd`/`historicalDataUpdate`(K 线流)。
- 订单:`orderStatus`、`openOrder`、`completedOrder`/`completedOrdersEnd`(重启后恢复当日已完成委托)、
  `execDetails`、`commissionReport`。
- 账户/持仓:`accountSummary(End)`、`position(End)`、`pnl`、`pnlSingle`。
- 深度:`updateMktDepth` / `updateMktDepthL2`。

**`IBKREngine`** — 高层封装(GUI 调用的 API):
- 连接:`connect(mode, host, ...)`、`disconnect`、`reconnect`、心跳 `_start_heartbeat`/`_on_heartbeat`、
  `_run_wrapper`(后台跑 `app.run()`)。
- 合约:`search_symbols`、`get_con_id`、`request_option_chain`、`resolve_option_con_id`、
  `_make_underlying_contract`/`_make_stock_contract`/`_make_option_contract`。
- 行情订阅:`subscribe_option_tick`(流式)、`snapshot_option_tick`(一次性快照, 配 `tickSnapshotEnd`
  自动清理, 不占常驻线)、`subscribe_stock_tick`、`unsubscribe_tick`、`get_tick`、
  `subscribe_market_depth`/`unsubscribe_market_depth`、`request_historical_data`/`cancel_historical_data`。
  错误 300(cancelMktData 找不到 tickerId, 多见于快照已自动取消)静默处理, 不再弹状态栏。
- 账户/持仓/盈亏:`request_account_summary`、`request_positions`、`request_pnl`、
  `request_pnl_single`(每仓位今日盈亏)及各自 `cancel_*`。
- **下单**:`place_limit_order`、`place_market_order`、`place_stock_order`、`place_forex_order`、
  `place_combo_order`(多腿组合)、`cancel_order`、`cancel_all_orders`、`close_position`。
- 拒单处理:`_on_order_error`(非用户撤单/纯警告才报错)、`_log_rejection`(追加写
  `logs/order_rejects_*.jsonl`,含拒单时刻盘口/持仓/在途订单快照)、`_on_order_status`、`_on_execution`、
  `_on_open_order`(重连/重启恢复 TWS 中**仍在挂的**订单, 也复用给完成单恢复)。
- 委托历史恢复:连接时 `reqOpenOrders`(当前挂单) + **`reqCompletedOrders(False)`(当日已成交/已撤销委托)**,
  `completedOrder` 经 `_option_from_contract`(支持 期权/正股/期货/组合)复用 `open_order_received` 进委托面板;
  跨会话完成单 `orderId` 可能为 0, 用 `permId` 兜底作主键。IBKR 仅提供「上次服务器重置以来」(约当日)的完成单。

### 4.4 模拟引擎 `paper_engine.py` (440 行)

- `PaperSignalBridge` 信号名与 `IBKRSignalBridge` 一一对应 → UI 无感切换。
- `PaperEngine(ibkr_engine)` 复用真实引擎的行情,本地按 tick 撮合限价单,维护模拟持仓/现金
  (起始 `PAPER_STARTING_CAPITAL=10000`),按 `config` 费率扣佣金。不做空。

### 4.5 主窗口 `main_window.py` (916 行)

`MainWindow(QMainWindow)`:
- `_build_ui()` 组装顶栏 SymbolBar、账户条 AccountBar,中央**顶层 `QTabWidget`**:
  「单腿点价」Tab(左侧 OptionChain、中间 PriceLadder、右侧 PositionPanel + OrderPanel 的 QSplitter 布局)
  与「多腿组合」Tab(嵌入的 `StrategyPanel`,独立于单腿点价);底部状态栏 + 交易时段指示。
- `_connect_signals()` 把 engine bridge 的全部信号连到对应槽。
- 交互槽:`_on_connect/_on_disconnect`、`_on_symbol_changed`(同步多腿面板标的)、
  `_on_mode_changed`(真实/模拟切换,同步多腿面板引擎)、`_on_center_tab_changed`(切到「多腿组合」懒加载链)、
  `_load_option_chain`、`_fetch_stock_price`(链头显示标的实时价)、`_on_option_selected`、
  `_on_contract_searched`/`_load_validated_contract`、`_on_order_requested`/`_on_market_order_requested`
  (买入前 `_resolve_buy_bracket` 决定是否附带止盈/止损: 期货强制、期权勾「随买入单附带」时;
  `_arm_buy_bracket` 挂出、`_trigger_price` 把期货点数换算绝对价、`_pending_buy_brackets` +
  `_on_exec_arm_bracket` 在成交回报后挂)、
  `_on_close_position_requested`、`_on_cancel_all_requested`/`_on_cancel_order`、
  `_on_open_chart`(懒加载 ChartWindow)、
  `_on_detach_ladder`/`_on_reattach_ladder`(点价梯独立窗口)、`_update_session_indicator`(SPX GTH/RTH/Curb)、
  `_on_error`/`_on_order_rejected`(弹窗 + 状态栏标红)、`closeEvent`。

### 4.6 widgets

| 文件 | 角色与要点 |
|------|-----------|
| `symbol_bar.py` (≈380) | 顶栏最左**「类型」三选一**(期权默认/正股/期货,`instrument_changed`)+ 期货**「合约月份」下拉**(`future_expiry_changed`,`populate_future_expiries`);代码搜索框(`QListWidget` 自动补全,走 `symbol_search_results`)+ 连接状态灯 + 模式 `QComboBox`(本地模拟 / IBKR模拟盘 / 实盘,item data 存 `TradingMode.value`,切到实盘弹确认);最右**「主题」下拉**(经典/科幻,`theme_changed` → 主窗口保存并提示重启,见 §5 主题行)。 |
| `option_chain.py` (≈520) | T 型报价表;按到期日分 Tab,顶部日期范围过滤(每范围最多 `MAX_EXPIRY_TABS_PER_RANGE` 个 Tab)+ **「🔄 刷新报价」按钮**;ATM 行高亮。**报价改用一次性快照**(`snapshot_option_tick`,切 Tab / 点按钮各拉一次,用完即弃**不占常驻行情线**),解决 Gateway 行情线紧张时整条链(含 TSLA)无数据;受 `MAX_SIMULTANEOUS_STREAMS` 限制每批快照数。**双击**某合约 → 打开该期权**当日 1 分钟图**(`chart_requested` → `option_chart_window.py`; 单击载入点价梯不变)。`select_expiry(expiry)`:外部(双击持仓/委托/监控的期权)联动选中指定到期日 Tab,不在当前 range 时自动切 range。 |
| `price_ladder.py` (★, ≈1500) | Futu 风格 5 列摆盘(我的买单/买量/价格/卖量/我的卖单)+ 深度条可视化;点击价格即下限价单;含合约搜索、数量选择、确认勾选、持仓摘要、市价买/卖/平仓、取消所有订单;tick size 由 `_tick_sizes()` 按品种(正股 penny / 期货 `FUTURES_SPECS` / 指数 `TICK_SIZE_OVERRIDES` / 期权 penny-pilot)给出;确认框单位按品种(张/股/手)。**「条件单」面板**:止盈/止损(可单选)+ 触发价/数量 + 本地或 IBKR 原生 + **标的价触发行** + 已挂列表;`conditional_requested`/`conditional_cancel_requested`/`option_loaded` 信号交主窗口接 `ConditionalOrderManager`。**两种用法**:「挂条件单」按钮对**当前持仓**挂;勾「**随买入单附带**」(`attach_to_buy()`)则开仓时按买入数量自动附带。**标的价触发**(仅期权):勾「标的价」+ 选方向(≥涨到/≤跌到)+ 填标的触发价 → 监控**标的**价, 到价即对本期权发**市价卖出**(本地监控; `arm(...,watch="UNDER",market=True)`)。**期货**条件单输入切到「**+点/−点**」(`_sync_cond_input_mode()`,相对入场价),`get_bracket(require_both)` 返回带 `by_points` 的配置;`open_cond_panel()` 展开面板。 |
| `watch_panel.py` (≈220) | **自选监控面板** (right_tabs 第三个 Tab「监控」, 与持仓/委托同窗口点击切换)。表: 合约/现价/⚠≥/⚠≤/✕; 点价梯左下「☆ 加自选」把当前合约加入并自动切到该 Tab; 现价 0.5s 刷新 (Tab 不可见时跳过重绘, 警报巡检照常); 双击警报列编辑触发价 (空=关, 一次性触发后自动清除); 触发 = 声音(`sound_alerts.play_alert`, sounds/ALERT 可自定义) + 非模态弹窗 + 行高亮 3s + 状态栏。逻辑在根目录 `watchlist.py` (WatchListManager)。 |
| `position_panel.py` (318) | 持仓表。**真实模式持仓全部来自 IBKR API**(`portfolio_position_received` = reqPositions + `reqPnLSingle` 盈亏),不依赖本地成交跟踪,故无幻影持仓/数目准;模拟模式来自 `PaperEngine` 本地撮合。显示未实现盈亏、今日盈亏、百分比、可按类型筛选;「费$」后缀 = 该合约**今日实际佣金**(`get_position_commission`)。 |
| `order_panel.py` (141) | 挂单/历史委托表;撤单按钮;拒单行标红,悬停看原因。**重启后自动加载当日已完成委托**(引擎 `reqCompletedOrders`,见 §4.3)。 |
| `account_bar.py` (≈270) | 账户摘要条,**两行布局**(窄屏单行会截断,故拆开):**第一行**=资金摘要(总资产/可用/购买力/未实现/今日盈亏 + 右侧**今日手续费**,`on_computed_daily` 取 `computed_daily_pnl` 信号的手续费分量,真实=IBKR commissionReport 日内累计,模拟=各笔估算佣金累计;**今日盈亏=IBKR dailyPnL**(较昨收、含费),仅当 dailyPnL 本会话从未有效时才用 已实现+未实现 兜底——两口径对隔夜仓差异大,不混用以免显示跳变);**第二行**=账户名(左)+ **美东时间实时时钟**(右,`_update_clock` 每秒刷新 `America/New_York`,无 tz 数据回退本地)。**币种余额条已从账户栏移除**(`on_currency_balance` 保留为空槽,数据仍在引擎侧流动,不动主窗口信号接线)。每 `ACCOUNT_REFRESH_MS` (3s) 调 `request_account_summary()` + `request_currency_balances()`,但这两者已**幂等**(订一次流式订阅, 之后调用直接返回, 不再 cancel+重订), 故定时器只是兜底、不再给 Gateway 制造 churn。 |
| `currency_balance.py` (≈70) | 各币种现金余额单行标签(`币种: EUR €414.00  USD $0.00`)。订阅引擎 `currency_balance_updated`(来自 `reqAccountSummary "$LEDGER:ALL"` 的 `CashBalance` 行);非零币种排前、含 0 余额也显示;**现仅正股 client 顶栏在用**(期权 GUI 账户栏已不再嵌)。 |
| `option_calculator.py` (≈610) | **期权理论价计算器**(主窗口右下角),**两列布局**。**左列「正向·理论价」**:跟随左侧待交易期权,用 IBKR 推送的 IV + 标的价 + 行权价 + 剩余到期时间跑 Black-Scholes 算「应有价格」,并与盘口中间价比对(偏贵标红/偏便宜标绿);QTimer 每 `CALCULATOR_REFRESH_MS`(700ms)刷新(随行情 + 时间衰减);取消「跟随实时」进入手动 what-if(改 S/IV/利率/天数)。**右列「反向·试算」**:顶部单选切换两个方向,共享一组参数(K/IV/r/到期):**①「期权价→标的价」**(原功能)改**目标期权价**,用单调二分法 `solve_underlying_for_price` 反推「期权要值目标价时标的需到的价位」,对比当前标的算需变动金额/百分比(↑绿/↓红),Put 目标价超 `K·e^(-rT)` 显示「无解」;**②「标的价→期权价」**(新增)改**假设标的价**,正算 Black-Scholes 期权价并与盘口中间价比对(相对盘口涨跌, ↑绿/↓红, 到期则取内在价值)。换合约时自动用实时值播种(目标价取盘口中价、假设标的取当前标的),「↺ 用实时值填充」可手动重置。正股伪合约两列均显示「仅期权适用」。 |
| `quantity_selector.py` (31) | 1–100 张数量微调器,emit `quantity_changed`。 |
| `strategy_defs.py` (196) | 纯数据:`StrategyType` 枚举 + `LegTemplate`/`StrategyTemplate` + `STRATEGY_REGISTRY`(牛/熊市价差、蝶式、铁鹰、铁蝶、跨式、宽跨、日历价差、自定义)。 |
| `strategy_window.py` (≈810) | **多腿组合策略生成器**。核心为可嵌入的 `StrategyPanel(QWidget)`,作主窗口「多腿组合」Tab:选模板 + 行权价/到期 → 生成各腿(实时刷新 bid/ask/净价/最大盈亏/佣金)→ `place_combo_order` 一键下 combo;`set_engine`/`set_symbol`/`ensure_loaded`(懒加载期权链)/`cleanup`(退订各腿行情)。`StrategyWindow(QMainWindow)` 为薄壳,兼容独立窗口用法。 |
| `chart_window.py` (879) | 懒加载 K 线窗口(numpy + pyqtgraph,约 25MB,故不在启动时导入);多周期(`CHART_TIMEFRAMES`)、MA5/20/50/200 + VWAP + 量柱、向左平移无限加载历史、实时流式/轮询更新。 |
| `option_chart_window.py` (≈280) | **期权当日 1 分钟图**(期权链**双击**某合约打开,`chart_requested` 信号);轻量独立窗口:蜡烛+量柱(X 联动),数据走 `request_option_historical_data`(1 min/1 D),数据源可切**成交价(TRADES)/中间价(MIDPOINT)**(稀疏合约中间价更连续、但无量),默认 10s 轮询自动刷新;顶栏显示 最新/涨跌(较今日首根开盘)/高/低;懒导入、`WA_DeleteOnClose`。**单实例**:双击新合约自动关旧图并停其取数,双击同一合约只把窗口带到前台(主窗口 `_option_chart` 管理)。 |
| `candlestick_item.py` (123) | pyqtgraph 自绘 OHLC,涨=空心绿、跌=实心红,cosmetic pen 任意缩放清晰。 |
| `chart_indicators.py` (38) | `IndicatorCalculator` 静态方法:`moving_average`、`vwap`(累积)、`volume_colors`。 |

> `option_chain.py` 的范围过滤桶:**0DTE / 本周 / 下周 / 本月 / 下月 / 远月 / 全部**;
> `position_panel.py` 与 `order_panel.py` 的筛选/上限:持仓可按 全部/期权/正股ETF 筛选,委托面板最多显示约 50 条。

### 4.7 诊断脚本 `check_spx_options.py` (329 行)
独立运行的探测脚本,用于核对 SPX/SPXW 期权合约定义与交易时段(GTH/RTH/Curb),非 GUI 依赖。
**用 clientId=99** 连 live(7496),只读、不下单,避免与 10/11 冲突。

### 4.8 期权组合分析器 `combo_analyzer.py` (独立程序, clientId=12)

独立 PyQt5 程序(`start_combo.bat`),复用 `IBKREngine`,专做**多腿期权组合**的价格分析与**原子交易**。

**功能:**
- **组合即时净价**:选标的 → 加载期权链 → 选策略(蝶式/铁鹰/铁蝶/跨式/宽跨/各类垂直价差/日历)
  → 自动按 ATM±翼展填各腿行权价(可逐腿手改)→ 显示组合净价(借记为正/贷记为负)与各腿最新价。
- **组合历史价(券商一般不提供)**:为每条腿调 `IBKREngine.request_option_historical_data`
  (期权合约历史 K 线,`formatDate=2` epoch 对齐),按时间戳**交集**合成
  `组合价 = Σ 各腿(BUY:+ / SELL:−)×ratio×close`,画价随时间变化的折线。
  数据类型可选 TRADES / MIDPOINT(期权流动性差时 MIDPOINT 更连续)。
- **组合 K 线(蜡烛, 多周期/多天)**:点「组合K线(蜡烛)」按上方「周期」拉各腿 OHLC(周期与时间跨度
  取自 `CHART_TIMEFRAMES`,如 1分≈1D / 5分≈1W / 1时≈1M / 4时≈1M / 日线≈1Y;**不再限当日**),
  用 `compute_combo_ohlc` 合并成组合**蜡烛图**(空头腿用其低/高反向贡献给组合高/低,给出价值包络),
  复用 `CandlestickItem`;x 轴 `%m-%d %H:%M` 时间刻度,滚轮缩放 / 拖动平移。
- **当日实时录制(零历史权限)**:点「▶ 录制当日」后, 每 2 秒用各腿实时盘口中价合成组合净价、
  累积成当日曲线实时画出 —— **不调用 `reqHistoricalData`, 只要有实时行情即可**, 适合无期权历史
  权限、只看当日的场景。某腿暂无报价时跳过该次采样。
- **绘图**:折线(历史/录制)与蜡烛(今日)共用一个 `PlotWidget`,统一**索引 x 轴 + 自定义时间刻度**。
- **连接模式**:顶栏「模式」下拉 = **IBKR模拟盘 (7497, 默认)** / **实盘 (7496)**。组合下单必须走真实
  IBKR 引擎(本地模拟不支持 BAG),故无「本地模拟」。默认连模拟盘,可端到端测试组合下单而不动真钱;
  选实盘时弹确认框。
- **组合原子交易**:用 IBKR 原生 **BAG 组合单**(`place_combo_order`)整组开仓,**整组成交**。
  - **持仓视为一个整体**:面板只提供「整组平仓」(反向 BAG 单)与「加仓」(同向 BAG 单),
    **不提供单腿平仓** —— 满足「同一组持仓,只能一起平仓或加仓」。
  - 组合分组 IBKR 账户侧不保留(会拆成各腿),故本地持久化到 `combo_positions.json`,重启恢复。
  - 持仓面板每秒由各腿实时盘口中价合成**现净价**与**盈亏**
    (BUY 组合 `(现−开)×组数×100`,SELL 组合反号)。
- **纯逻辑** `widgets/combo_pricing.py`:`combo_price_from_prices` / `compute_combo_series`
  / `auto_assign_strikes` / `resolved_legs`,无 Qt/IBKR 依赖,可单测。
- 日志:`logs/combo_app_YYYY-MM-DD.log`。

> ⚠️ 「计算组合历史价」依赖账户的**期权历史数据权限**(与实时/快照行情是**独立订阅**);
> 是否具备可用 `check_option_history.py` 实测。无权限时改用「▶ 录制当日」(实时累积, 零历史权限)。
> 本程序只发组合单、从不单腿下单;但无法阻止用户在别的 GUI 里单腿操作这些合约。

---

### 4.9 Gateway 版入口 `main_gw.py` (新版本, 缓解 TWS 崩溃)

**为什么有这个**:TWS(Trader Workstation)是重型 Java GUI,长跑易卡顿/崩溃。本项目典型用法是
期权 GUI(clientId=10)+ 正股 GUI(clientId=11)同开,各自加载期权链时每个到期日 tab 订 ~62 行
行情,**合计逼近甚至超过账户 ~100 行的行情上限**,叠加 TWS 自身的界面渲染负担 → 崩溃高发。

**新版怎么做的**(全部由环境变量 `IBKR_USE_GATEWAY=1` 驱动,在 `config.py` 导入时求值):
- **连 IB Gateway**(纯 API 网关,几乎无界面、内存约为 TWS 一半)而非 TWS:端口
  `4001`(live)/ `4002`(paper)。
- **收紧行情订阅**:`MAX_SIMULTANEOUS_STREAMS` 95→45、`CHAIN_STRIKES_AROUND_ATM` 15→10
  (期权链显示/订阅 ATM±10=21 档),使期权+正股两个 GUI 合计稳在 100 行以内。
- **与旧版完全隔离**:入口文件名独立(`main_gw.py`)→ `kill_previous_instances` 只杀本脚本旧
  进程,**不会动正在运行的 `main.py`**;日志写 `logs/app_gw_*.log`;窗口标题加 `[GW 新版]`。
  旧版 `start.bat`/`main.py` 不设环境变量 → 行为与之前**字节级一致**(TWS + 95/15)。

**启动**:先开并登录 **IB Gateway**(API → Socket Port 确认 4001/4002、勾选 Enable
ActiveX and Socket Clients),再双击 `start_gateway.bat`。在 GUI 顶栏选「IBKR模拟盘」即连 4002
模拟账户测试。注意:同一账户 TWS 与 Gateway 通常二选一登录(同账户重复登录会互踢)。

**回退**:有任何问题直接用旧 `start.bat`(TWS)即可,新版无需卸载、互不影响。

---

## 4.8 线程与定时器一览

| 组件 | 线程 | 机制 |
|------|------|------|
| IBKR API 回调 | reader(daemon) | `app.run()` 循环,EWrapper 回调在此线程触发 |
| Qt GUI | 主线程 | 事件循环,槽函数接收 bridge 信号 |
| 跨线程 | bridge | `pyqtSignal`(线程安全)从 reader → GUI |
| 阻塞请求 | 主线程 | `threading.Event` + 超时(合约/链 ~10s) |
| 模拟撮合 | 主线程 | `PaperEngine` QTimer 500ms 检查挂单成交 |
| 账户刷新 | 主线程 | `AccountBar` QTimer `ACCOUNT_REFRESH_MS`(3s) |
| 报价刷新 | 主线程 | `OptionChain`/`PriceLadder` QTimer ~1s |
| 心跳/超时 | 引擎 | `_on_heartbeat`,~30s 无 tick 告警;clientId 占用(err 326)按 +10 重试 |

---

## 5. 配置 `config.py` 速查

| 类别 | 关键常量 |
|------|---------|
| 连接 | `IBKR_HOST=127.0.0.1`;TWS `7496`(live)/`7497`(paper);Gateway `4001`(live)/`4002`(paper),可用 `IBKR_GW_LIVE_PORT` / `IBKR_GW_PAPER_PORT` 环境变量覆盖;`IBKR_CLIENT_ID=10`、`IBKR_COMBO_CLIENT_ID=12` |
| 行情 | `MARKET_DATA_TYPE=1`(1实时/2冻结/3延迟/4延迟冻结);`MAX_SIMULTANEOUS_STREAMS=95` |
| Tick | `TICK_SIZE_SMALL=0.01`/`TICK_SIZE_LARGE=0.05`/`TICK_THRESHOLD=3.0`;`LADDER_ROWS=201`;`TICK_SIZE_OVERRIDES`(SPX/XSP/NDX/RUT/SPY/QQQ/IWM) |
| 深度 | `DEPTH_ROWS=10` |
| 费率 | 期权 `$0.65/张`,`min $1.00`;正股 `$0.005/股`,`min $1.00`;模拟起始资金 `$10000` |
| 时段(ET) | SPX GTH 20:15→09:15,RTH 09:30→16:15,Curb 16:15→17:00;`EXTENDED_HOURS_SYMBOLS={SPX}` |
| 代码 | `INDEX_SYMBOLS`(secType=IND);`DEFAULT_SYMBOLS`(SPY/SPX/QQQ/...) |
| 期货 | `FUTURES_SPECS`(根代码→交易所/乘数/tick/名称: ES/MES/NQ/MNQ/RTY/M2K/YM/MYM/CL/MCL/GC/MGC);`FUTURES_SYMBOLS`;`FUTURES_MAX_EXPIRIES=5`(月份下拉档数);`FUTURES_COMMISSION_PER_CONTRACT/MIN`。只收录 tick≥0.01 的品种(点价梯按 2 位小数网格)。 |
| 错误码 | `IGNORED_ERROR_CODES`(静默)/`DATA_CONNECTION_ERROR_CODES`(2100/2103-2108 作警告上抛) |
| 期权定价 | `RISK_FREE_RATE=0.045`、`DIVIDEND_YIELD=0.0`、`OPTION_MARKET_CLOSE_ET=16`、`CALCULATOR_REFRESH_MS=700`(计算器用) |
| 图表 | `CHART_TIMEFRAMES`(1秒~月线)+ 各类颜色 |
| 主题 | `THEMES`: `classic`(经典深蓝, 原版配色)/`scifi`(简约科幻: 近黑底+电光青+霓虹绿红, 高对比文字, 直角, Bahnschrift 字体)/`light`(亮色: 白底+浅灰蓝面板+深藏青文字, 绿/红加深保证白底可读)。启动时 `load_theme_name()` 读 `theme.json`(运行时文件, 已 gitignore)后 `globals().update` 注入全部 `COLOR_*`/`FONT_FAMILY`/`FONT_SIZE`/`UI_RADIUS` —— 下游 206 处 `setStyleSheet` 引用零改动。原散落各 widget 的硬编码色(点价梯最优买/卖价块、深度条高亮、警报行闪烁、accent hover)已收编为主题键(`COLOR_BID_PRICE_BG` 等, classic/scifi 逐值保留)。K 线图表走独立 `CHART_COLOR_*` 深色常量, 不随主题变。顶栏「主题」下拉切换 → `save_theme_name` + 提示自动重启生效(内联样式构建时固化, 无法热切)。 |

---

## 6. 日志与拒单排查

- **运行日志**:`logs/app_YYYY-MM-DD.log`(期权 GUI)/`logs/combo_app_YYYY-MM-DD.log`(组合分析器),
  收纳 pythonw 下所有 `print`/traceback。
- **拒单日志**:`logs/order_rejects_YYYY-MM-DD.jsonl`,每行一个 JSON,含
  `time/mode/order_id/contract/action/qty/order_type/limit_price/reject_code/reject_msg`
  + 拒单时刻盘口(bid/ask/last)+ 当前持仓数量 + 其他在途订单快照。
  **分析方法**:让 Claude 直接读该 jsonl 即可定位拒单原因。
- **常见拒单**:同合约第二笔被 TWS *Duplicate Order Precaution* 拦截(API 订单无法回应确认框,TWS 自动拒)。
  一次性修复:TWS → Global Configuration → API → Precautions → 勾选
  **"Bypass Order Precautions for API Orders"**。

---

## 7. TWS Workstation API 与 IB Gateway 的区别

两者**用的是同一套 IBKR API(EClient/EWrapper,即本项目用的 `ibapi`)**,客户端代码完全不用改,
**唯一实质差异是默认端口**。区别在于"宿主程序"不同:

| 维度 | **TWS (Trader Workstation)** | **IB Gateway** |
|------|------------------------------|----------------|
| 本质 | 完整的图形化交易终端(人用 + API) | 纯 API 网关,**几乎无界面**(只有一个连接状态小窗) |
| 图形界面 | 有:报价、图表、下单面板、风控弹窗等全套 | 无:不能手动看盘/手动下单 |
| 默认端口 | **7496**(live)/ **7497**(paper) | **4001**(live)/ **4002**(paper) |
| 资源占用 | 重(Java GUI,内存/CPU 高) | 轻(无渲染,适合服务器/长跑/无人值守) |
| 稳定性 | GUI 偶发卡顿可能影响 API | 更精简,长时间运行更稳 |
| 自动重启/维护 | 每日需重新登录(可设自动重连) | 同样有每日重启,但更适合配合 IBC 等自动化登录 |
| API 设置位置 | Global Configuration → API → Settings | 同样的 API 设置页,但整个程序就是为 API 服务 |
| 适用场景 | **既要手动盯盘又要 API**(本项目当前用法) | **纯自动化/服务器部署**,不需要人看界面 |
| 行情订阅 | 与 Gateway 共享同一账户的行情权限 | 同上,权限取决于账户订阅,而非用哪个宿主 |

**对本项目的含义:**
- 当前 `config.py` 默认连 **TWS 7496(live)/7497(paper)**;若改用 Gateway,端口为
  **4001(live)/4002(paper)** (IB Gateway 出厂默认),其余代码(`IBKRApp`、下单、回调)一字不改。
  现在已内置开关:用 `start_gateway.bat` / `main_gw.py` 启动即设环境变量 `IBKR_USE_GATEWAY=1`,
  自动切到 Gateway 端口(详见 §4.9)。
- **行情数据包**与用 TWS 还是 Gateway **无关**——取决于账户购买的行情订阅(本项目已购美股快照)。
- **同一时刻**对一个账户,TWS 与 Gateway **通常二选一登录**(同账户重复登录会互踢);但**同一个宿主**
  下可以让**多个 client(不同 clientId)并行**——本项目正是用一个 TWS 同时接 clientId=10(期权)
  与 11(正股)。
- 选型建议:需要一边人工看盘一边跑这套 GUI → 用 **TWS**;要把它丢到服务器纯自动跑、省资源 → 用 **Gateway**
  (配合 IBC 实现自动登录与每日重启)。

---

## 8. 变更记录 (Changelog)

> 倒序排列,最新在上。每次改动本目录代码后追加一行:**日期 — 一句话说明(涉及文件)**。

- **2026-08-21 — v1.2.0 公开版**:同步私有开发版自 v1.1.0 后的稳定改进,并完成公开发布审查。
  主要包括:行情订阅 reqId 全进程唯一与重复 ticker 自动重订;行情停滞自愈;自选启动即加载并支持
  多条警报;委托显示实际成交均价;期权/正股一键平仓二次确认;非美元持仓与账户基础币种统一折算;
  总资产实时外推与定期真值校准;自适应布局、三套主题、输入法卡顿规避及安装脚本。公开版移除了
  机器专用端口和私有运维说明,Gateway 端口恢复官方默认并支持环境变量覆盖。
  (`config.py`, `ibkr_engine.py`, `main_window.py`, `models.py`, `paper_engine.py`, `watchlist.py`,
  `widgets/`, `setup.bat`, `requirements.txt`, `sounds/ALERT.wav`)

- **2026-07-17** — **`setup.bat` 的 python 检查改为验版本+验 pip**;**修正 `start_gateway.bat`
  写反的端口注释**。原检查只有 `python --version >nul` 判存在,当 PATH 中另一个较旧且没有 pip 的
  Python 发行版排在前面时会误判通过,然后才在 pip 阶段报
  `No module named pip`, 报错指向完全错误的方向。现在改为解析版本号 (`<3.13` 报 `:badpy`)、
  再验 `python -m pip` (`:nopip`),并在报错里点破根因:**Windows 拼 PATH 是「系统级在前、用户级
  在后」,装 Python 勾的 "Add to PATH" 只加到用户级,会被系统级的旧 python 压住**。
  另 `start_gateway.bat` 注释原写 "4002=实盘, 4001=模拟盘",**两个端口写反了**;
  IB Gateway 官方默认是 4001=实盘 / 4002=模拟盘。仅注释错误、不影响运行。
  (`setup.bat`, `start_gateway.bat`)

- **2026-07-15** — **监控 Tab: 每个标的支持任意多条到价警报 (右键追加)**。原每合约仅一条
  高于价+一条低于价, 改为**警报列表**: 数据模型新增 `Alert` (方向 above/below + 价 + 会话内
  id), `WatchItem.alert_above/below` 标量 → `alerts: list[Alert]` (`to_dict` 存 `alerts`,
  `from_dict` 兼容并迁移旧标量格式)。Manager 换 `set_alert` 为 `add_alert/update_alert/
  toggle_alert_direction/remove_alert`, `_tick` 逐条巡检、命中即移除 (仍一次性)。面板改为
  **每条警报一行** (合约无警报时占一行占位): **右键任一行** → 「添加涨破/跌破警报」(弹价格框,
  可无限追加, 如 TSLA 395/400/405) / 「移除该合约监控」; 单击「条件」列切 ≥/≤, 双击「警报价」
  编辑 (留空/0 删该条), ✕ 删该条警报或占位行删整个合约; 触发时该合约所有行高亮。
  (`watchlist.py`, `widgets/watch_panel.py`)
- **2026-07-13** — **新增「亮色」主题 (白底) + 残留硬编码色收编主题化**。`THEMES` 加第三套
  `light`: 白底/浅灰蓝面板 + 深藏青文字, 绿/红/琥珀加深保证白底可读, 行高亮改浅色配深字;
  顶栏「主题」下拉三选一。同时把散落各 widget 的深色硬编码收编为主题键(classic/scifi
  逐值保留, 零视觉回归): 点价梯最优买/卖价块底/字色、深度条行高亮/填充/文字高亮、自选
  警报行闪烁色、accent 按钮 hover(`COLOR_ACCENT_HOVER`)、strategy_window 暗字/禁用态、
  ES 动量震荡琥珀色(复用 `COLOR_MY_ORDER`)。K 线图表保持独立 `CHART_COLOR_*` 深色不随
  主题变。(`config.py`, `widgets/price_ladder.py`, `widgets/option_chain.py`,
  `widgets/watch_panel.py`, `widgets/strategy_window.py`, `widgets/symbol_bar.py`)
- **2026-07-13** — **新增「简约科幻」主题, 顶栏可在经典/科幻间切换**。`config.py` 改为
  `THEMES` 双主题字典 + `theme.json` 持久化, 启动时把选中主题的 `COLOR_*`/`FONT_FAMILY`/
  `FONT_SIZE`/`UI_RADIUS` 注入模块全局 —— 15 个 widget 文件的 206 处样式引用零改动,
  经典主题逐值保留原配色。科幻主题: 近黑深蓝底 + 电光青 accent + 霓虹绿/红, 文字提亮
  (#e6f1ff)对比更清晰, 直角边框, Bahnschrift(DIN 风格)尖锐字体; `DARK_STYLESHEET` 追加
  选中 Tab 顶部青色描边/输入框聚焦青边/表头字距等科幻专属规则。顶栏最右「主题」下拉
  切换 → 保存 + 询问立即自动重启(内联样式构建时固化, 需重启生效); combo_analyzer 入口
  字体同步走主题配置。(`config.py`, `main_window.py`, `widgets/symbol_bar.py`,
  `widgets/price_ladder.py`, `main.py`, `main_gw.py`, `combo_analyzer*.py`, `.gitignore`)
- **2026-07-13** — **双击期权合约联动跳到该到期日 Tab**。双击持仓/委托/监控里的期权
  (C/P) 时, 期权链不再停在默认到期日: 同标的直接 `select_expiry` 选中该合约到期日的
  Tab (不在当前 range 时自动切到第一个包含它的 range); 换标的则经
  `_load_option_chain(jump_expiry=...)` 在链加载完成后跳。正股/期货/COMBO 不受影响。
  (`widgets/option_chain.py` 新增 `select_expiry`, `main_window.py`)
- **2026-07-10** — **监控 Tab 双击合约名跳点价梯/期权链**。与持仓/委托面板同款交互:
  双击「监控」页某行的合约名, 载入该合约到点价梯+计算器, 期权自动切标的并重载期权链,
  正股/期货切对应品种模式; ⚠≥/⚠≤ 列双击仍是编辑警报价, 互不干扰。
  (`widgets/watch_panel.py` `option_selected`, `main_window.py`)
- **2026-07-10** — **修非美元持仓市价/盈亏%显示错误**。根因: IBKR
  `position` 回调的 avgCost 是**当地货币**, 而 `reqPnLSingle` 的
  value/unrealizedPnL 是**基础货币 USD** — 面板把 USD 市值直接除以数量当"当地股价"、
  把 USD 盈亏除以 TWD 成本算盈亏%, 全部错乱。修复: 非 USD 标的市价按
  `均价×市值/(市值−浮盈亏)` 换算回当地货币; 盈亏% 一律用 USD 成本基础 (市值−浮盈亏);
  均价/市价列非美元时标注货币代码,市值/盈亏保持 USD。
  (`models.py` `pnl_pct`, `widgets/position_panel.py`)
- **2026-07-10** — **修 Put 价差腿方向反了 + 多腿组合一键平仓**。① `strategy_defs.py` 两个
  Put 垂直价差的腿与「strike1=低/strike2=高」约定不符: Bear Put 应**买高卖低**却买低卖高
  (下出来实为 Bull Put 信用价差), Bull Put 应**卖高买低**却卖低买高 — 两者恰好互换, 已修正
  (Call 价差/蝶式/铁鹰/跨式检查无误)。② 多腿组合本就以 IBKR **BAG 组合单**整体成交 (视为一个
  整体买入); 新增「已开组合 (一键平仓)」区: 下单后自动记录 (持久化 `strategy_combos.json`,
  gitignore, 成交标「持仓中」, 撤/拒单自动移除, 过期清理), 点「一键平仓」以**反向市价 BAG 单**
  整体平掉 (确认框列明细); `place_combo_order` 新增 `market` 参数支持市价组合单。
  (`widgets/strategy_defs.py`, `widgets/strategy_window.py`, `ibkr_engine.py`, `.gitignore`)
- **2026-07-10** — **修正股碎股显示「数量 0 但有市值」**。持仓面板把 API 数量 `int()` 取整,
  分红再投等产生的碎股 (如 0.6656 股) 显示成 0。改为保留原始小数: 整数正常显示, 碎股显示
  最多 4 位小数 (如 0.6656)。(`widgets/position_panel.py`)
- **2026-07-10** — **每笔提交后数量复位 1 + 卖空无条件拦截**。① 点价梯限价买卖/市价买卖/
  平仓任一笔提交成功后, 「数量」自动复位为 1 (防上一笔的大数量残留误下)。② 防卖空从
  「弹确认框可放行」升级为**无条件拦截**: 卖出量 > API 持仓的点价梯卖单直接拒绝并弹提示
  (无放行按钮); 多腿组合的价差空腿不经此处、不受影响。(`main_window.py`
  `_confirm_sell_to_open`, `widgets/price_ladder.py` `reset_quantity`)
- **2026-07-10** — **「标的价」条件单支持监控任意标的 + 巡检加密到 0.2s**。① 「标的价」行
  新增监控标的下拉 (默认「自己」= 本期权的标的; 预置 SPX/SPY/ES/NQ/QQQ/XSP/NDX, 可手输任意
  代码), 如 SPY 期权可设 SPX 或 ES 到价市价卖出; ES/NQ 等期货自动解析近月合约订阅
  (`ibkr_engine.subscribe_watch_tick`, 后台解析不卡 GUI, 行情统一落 `__stock__SYM` 键)。
  换监控标的时触发价清零并按新标的现价重新播种; 挂单确认/已挂列表均显示监控标的名。
  `ConditionalOrder` 新增 `watch_symbol` 字段 (持久化兼容旧 json)。② 条件单巡检间隔
  500ms → **200ms** (用户要求)。手动卖出自动取消条件单时行情订阅一并退订 (原有逻辑,
  含监控标的行情线)。(`models.py`, `conditional_orders.py`, `ibkr_engine.py`,
  `paper_engine.py`, `main_window.py`, `widgets/price_ladder.py`)
- **2026-07-10** — **手动卖出自动取消该合约条件单 + 修心跳定时器从未运行**。① 用户要求:
  点价梯手动卖出 (限价/市价/平仓) 成功提交后, 自动取消该合约全部本地条件单并在状态栏提示
  (残留条件单会在重新买入同合约时按旧触发价把新仓位卖掉); 条件单自身触发的卖出不受影响。
  (`main_window.py` `_cancel_conds_on_manual_sell`) ② 日志排查发现: 心跳 QTimer 在后台
  connect 线程创建, 违反 Qt 线程约束 → **定时器从未真正运行** (reader 线程死亡/行情超时监控
  一直失效), 且每次连接/断开报 QObject::startTimer/killTimer 警告。改为 bridge.connected
  信号驱动、在 GUI 线程启动; `_stop_heartbeat` 跨线程调用时经 QMetaObject.invokeMethod
  队列到定时器所属线程。(`ibkr_engine.py`)
- **2026-07-10** — **修条件单「标的价」残留勾选导致期权被立即卖出**。挂止损/止盈时,
  「标的价」勾选残留 + 触发价被自动播种为**当时标的现价** → 静默一起挂出一条
  「标的 ≥现价 → 市价卖」并可能立即触发。
  三重修复 (`widgets/price_ladder.py`): ① 点「挂条件单」后**取消全部勾选** (止盈/止损/标的价,
  用户要求) 并清零标的价输入, 每次挂单必须重新明确勾选; ② 挂单前核对各腿, 条件在**当前价下
  已满足** (挂上即秒卖) 时列明细弹确认框 (默认 No); ③ 换合约时清空旧合约的触发价与勾选,
  面板开着则按新合约现价重新播种 (播种逻辑抽为 `_seed_cond_prices`)。
- **2026-07-10** — **修自选到价警报反复响 + RuntimeError**。根因: 触发警报后 `_highlight_row`
  的 `setBackground` 触发 `itemChanged`, 被 `_on_item_changed` 当成用户编辑, 把刚清零的警报价
  **重新设回去** → 每 0.5s 巡检反复触发; 且触发后表格重建销毁旧单元格, 3 秒后取消高亮的定时器
  拿着失效引用报 `RuntimeError: QTableWidgetItem has been deleted`。修复: 高亮改走
  `_set_row_background` (设背景期间置 `_rebuilding` 屏蔽 itemChanged; 取消高亮时按 key 重新查行,
  行没了静默跳过), 且警报高亮延到表格重建后再画 (原先立即画会被重建抹掉)。现在到价只提醒一次。
  (`widgets/watch_panel.py`)
- **2026-07-09** — **防误卖空: 点价梯卖单超持仓时弹确认框**。点价梯停在未持仓合约时
  误点「市价卖出」可能意外开出裸空腿。新增 `_confirm_sell_to_open`: 点价梯发起的
  限价/市价**卖单**先按 API 持仓核对, 卖出量 > 持仓 (= 会开空) 时弹「将开出空头仓位」确认框
  (默认 No; 持仓快照未就绪按 0 对待)。市价平仓按钮/条件单已有各自校验, 多腿组合 combo 不经此处。
  (`main_window.py`)
- **2026-07-09** — **到价警报音换成 misaka 语音**。用 GPT-SoVITS misaka v3 生成
  `sounds/ALERT.wav`(日语「価格到達よ」, 1.6s), `play_alert()` 自动优先播放它 (无需改代码);
  也可替换为自制的 `ALERT.wav` / `ALERT.mp3`。
  (`sounds/ALERT.wav` 新增, `sounds/README.md`)
- **2026-07-09** — **新增「自选监控 (watch list)」+ 修点价梯盘口空档消失**。
  ① **自选监控**: 点价梯左下角新增「☆ 加自选」按钮 (`watch_requested` 信号), 把当前合约加入
  **「监控」Tab** (`widgets/watch_panel.py`, right_tabs 第三页, 与持仓/委托同窗口点击切换,
  加自选后自动切到该页); 现价 **0.5s 刷新**
  (Tab 不可见时跳过重绘); 每条可双击设置 **高于(⚠≥)/低于(⚠≤) 到价警报** —— 触发 = 警报音
  (`sound_alerts.play_alert`, `sounds/ALERT.wav` 可自定义, 缺省三连蜂鸣) + 非模态弹窗 +
  行高亮 + 状态栏, **一次性** (触发后该方向自动清除); 手动 ✕ 删除; 持久化 `watchlist.json`
  (gitignore), **启动 resume 时自动删除过期合约** (期权 expiry<今日, 期货月份<本月, 正股不过期)。
  逻辑在 `watchlist.py` (WatchListManager, 与条件单管理器同构: 注入 get_tick/订阅/退订,
  连接 resume / 断开 suspend)。
  ② **点价梯红绿盘口空档不再消失**: L2 深度流批量 delete/断流或 bid_size/ask_size 推 0 时,
  红绿量条会整体消失几秒 —— 现缓存上一份有效盘口档位与量 (`_last_bid_map/_last_ask_map/
  _last_bid_sz/_last_ask_sz`), 无新数据时保持显示, 换合约才清空 (与既有 `_last_bid/_last_ask`
  价格缓存同思路)。
  (`watchlist.py` 新增, `widgets/watch_panel.py` 新增, `widgets/price_ladder.py`,
  `main_window.py`, `sound_alerts.py`, `.gitignore`)
- **2026-07-09** — **修期权分时图蜡烛「黏连」**。`CandlestickItem` 原样式 = 阳线空心轮廓 +
  主体 1px cosmetic 描边; 全天 ~390 根 1min 挤在 ~900px 时每根仅 ~2px, 描边不随缩放变细,
  轮廓与影线糊成一片、分不出实心主体和细线。加 `solid=True` 模式: 涨跌主体都**纯填充不描边**
  (影线仍 1px 细线, 十字星画横线), 期权分时图启用; 正股 ChartWindow 保持原空心样式不变。
  离屏渲染 390 根/60 根两档验证: 放大后为标准「实心主体+细影线」。
  (`widgets/candlestick_item.py`, `widgets/option_chart_window.py`)
- **2026-07-09** — **修退出崩溃: OptionChartWindow 缺 `cleanup()`**。主窗口 closeEvent 对
  `_chart_windows` 里所有图表统一调 `cleanup()`, 期权分时图窗口只有 closeEvent 没有该方法 →
  开着期权分时图退出程序时 AttributeError, 后续 cond_manager/engine 的清理与断开被跳过
  (18:13 实盘日志捕获)。补 `cleanup()`(停轮询+丢弃迟到数据)。(`widgets/option_chart_window.py`)
- **2026-07-09** — **修条件单三缺陷 (实盘拒单日志定位)**。7/9 实测: 标的价条件单触发正常,
  但用户已手动平仓 → 照原数量发市价卖单 = 开裸空 → IBKR 201 保证金拒单; 另有 7/8 已到期的
  TSLA 条件单残留、次日标的到价时触发被拒 "Order is already expired", 且同一条件被重复挂两条。
  修复: ① **触发时先按 API 持仓核对** —— `configure()` 新增 `get_position_qty`/`positions_ready`
  回调 (真实引擎 `get_position_qty` 走 reqPositions 真相 + 新增 `positions_synced` 属性,
  positionEnd 快照到达前触发暂缓 1s 重查, 防止启动初期误判空仓; PaperEngine 恒 True),
  持仓 0 → 条件单自动作废 (新 `voided` 信号 → 状态栏提示), 持仓不足 → 数量夹到实际持仓;
  ② **过期清理** —— `ConditionalOrder.is_expired()` (期权 expiry < 今日), `_load` 恢复时丢弃
  并写回磁盘, `_check` 巡检时也作废 (盖住程序长开跨日的场景); ③ **arm 去重** —— 同合约+
  同类型+同监控对象+同方向+同触发价的旧条件单被新单替换, 不再并存连发两单。
  (`conditional_orders.py`, `models.py`, `ibkr_engine.py`, `paper_engine.py`, `main_window.py`)
- **2026-07-06** — **期权链双击合约 → 该期权当日 1 分钟图**。新增轻量窗口
  `widgets/option_chart_window.py`: 蜡烛+量柱(X 轴联动), 数据走已有
  `request_option_historical_data`(1 min / 1 D, 工作线程阻塞拉取), 数据源可切
  成交价(TRADES)/中间价(MIDPOINT), 默认 10s 轮询自动刷新, 顶栏显示最新价/涨跌/高低;
  期权链加 `cellDoubleClicked` → `chart_requested` 信号(单击载入点价梯的行为不变),
  主窗口懒导入开窗(与正股 ChartWindow 同列表管理)。**单实例**: 同时只保留一个期权
  分时图 —— 双击新合约自动 close 旧合约窗口(closeEvent 停轮询定时器、迟到数据丢弃、
  `WA_DeleteOnClose` 销毁释放); 双击同一合约则把已开窗口带到前台不重开
  (`MainWindow._option_chart` 单实例引用 + `_on_option_chart_destroyed` 清理)。
  实盘离屏验证: SPY 0DTE 拉到 171 根当日 1min bar, 时间轴 09:30→盘中 (ET);
  close 后定时器立停、destroyed 正常触发。
  (`widgets/option_chart_window.py` 新增, `widgets/option_chain.py`, `main_window.py`, `README.md`)
- **2026-07-06** — **修两处盈亏/手续费显示 bug (实盘实测验证)**。
  ① **点价梯/持仓面板「手续费」真实模式恒 0**: 真实引擎 `get_position()` 构造的 `PositionInfo`
  从不填 `total_commission`。新增 `IBKREngine.get_position_commission(key)`: `execDetails` 记
  execId→conId, 把 `commissionReport` 的**实际佣金**按合约归集(按 execId 去重、跨日清零),
  返回该合约**今日**已付佣金(隔夜仓的开仓佣金已含在 IBKR avgCost 内, 不重复);
  `PaperEngine` 提供同名接口(本地估算累计); 点价梯持仓摘要与持仓面板「费$」改走该接口。
  ② **账户栏「今日盈亏」两口径跳变**: IBKR dailyPnL(较昨收、含费)偶尔推 DBL_MAX→NaN,
  旧逻辑此时切到「已实现+未实现」兜底 —— 该口径把隔夜仓今日之前的浮亏也算进"今日",
  持有隔夜仓时两数相差可达数十美元, 显示在两个数之间来回跳、看似算错。现在 dailyPnL
  一旦有效过就只用它(NaN 保留上次好值), 兜底仅用于 dailyPnL 整个会话从未有效时;
  并给标签加口径 tooltip。(`ibkr_engine.py`, `paper_engine.py`, `widgets/price_ladder.py`,
  `widgets/position_panel.py`, `widgets/account_bar.py`)
- **2026-07-01** — **条件单新增「标的价触发 → 市价卖出」**。买了期权后, 可挂「监控**标的**价、
  到达触发价即对本期权发**市价卖出**指定数量」的条件单(如买 SPX 7500C, 设「≥ 7510」→ SPX 涨到 7510
  自动市价卖)。做法: `ConditionalOrder` 加 `watch`(SELF/UNDER)/`direction`(UP/DOWN)/`market` 三字段
  + `watch_key`(标的看 `__stock__SYM`)/通用 `is_triggered`; `ConditionalOrderManager.arm` 加同名参数、
  监控循环用 `watch_key` 取价、`configure` 加 `subscribe_under` 订标的行情、`_place` 回调多传 `market`;
  点价梯条件单面板加「标的价」行(方向 ≥涨到/≤跌到 + 触发价, 仅期权可见, 开面板按现标的价+CALL/PUT
  播种方向); `main_window` 的 place 回调按 `market` 选 MKT/LMT、`subscribe_under` 走 `subscribe_stock_tick`。
  仍是**本地监控**(仅程序运行时有效, 已持久化重连恢复), 触发后走市价单。
  (`models.py`, `conditional_orders.py`, `main_window.py`, `widgets/price_ladder.py`)
- **2026-06-30** — **利率行短端 13周(IRX) → 2年(Yahoo `2YY=F`, 延迟) + 账户栏改两行、删币种条**。
  ① **计算器利率行**: 短端档位从 13周(IRX) 换成 **2 年期**。CBOE 无标准 2 年收益率指数, 故改从
  Yahoo Finance `2YY=F`(CBOE 2 年期收益率期货, `regularMarketPrice` 即收益率%、无需换算)经
  **daemon 线程**拉取(`_fetch_2y_yield`, urllib 超时 6s), 结果用 `_rate2y_ready` 信号回 GUI 线程,
  标签旁标注「**延迟**」(Yahoo 非实时)。5年/10年仍走 IBKR CBOE 指数。`_RATE_SYMBOLS` 去掉 IRX、
  `INDEX_SYMBOLS` 注释更新(IRX 仍留作 IND 注册)。
  ② **账户摘要条改两行**(窄屏单行会截断): 第一行=资金摘要(总资产/可用/购买力/未实现/今日盈亏/手续费),
  第二行=账户名(左)+ 美东时钟(右)。**移除币种余额条**(`on_currency_balance` 留空槽, 不动主窗口信号接线)。
  (`widgets/option_calculator.py`, `widgets/account_bar.py`, `config.py`)
- **2026-06-29** — **账户摘要条右上角(时钟左侧)新增「今日手续费」统计**。复用已有 `computed_daily_pnl`
  信号的**手续费分量**(今日盈亏分量仍弃用)。真实模式: IBKR `commissionReport` 按 `execId` 去重、日内
  累计、跨日清零, 重启后经 `reqExecutions` 补齐当日历史成交手续费; 模拟模式: `PaperEngine` 新增
  `_today_commission` 累计每笔成交估算佣金, 经同一信号上报(原来恒推 0)。`account_bar.on_computed_daily`
  改为只用 commission 分量驱动新标签(NaN/DBL_MAX 守护); `stop()` 切换/断开时清回「--」。
  (`widgets/account_bar.py`, `paper_engine.py`)
- **2026-06-26** — **期权链标题在标的价右侧显示标的 IV (如 `SPY $730.76  IV 18.2%`)**。
  标的隐含波动率 = IBKR 对该标的计算的「期权隐含波动率」(TWS 的 Implied Vol %)。做法: 期权链的标的行情
  订阅 (`_fetch_stock_price`) 加 **genericTick 106**, 引擎 `tickGeneric` 处理 **tickType 24** → 存
  `_tick_data['iv']`; `_refresh_prices` 把它拼到标题价格右侧 (小数×100 显示百分比)。指数(SPX 等)可能不下发
  则不显示。(`ibkr_engine.py`, `main_window.py`, `widgets/option_chain.py`)
- **2026-06-26** — **ES 动量指示加「趋势/震荡」regime (5MA 斜率法, 移植 slope.py)**。
  在原翻转(翻多▲/翻空▼)基础上, 加 Vordinkkk 的 chop/trend 判定: 5周期均线、最近5根斜率同向占比
  ≥0.8 → 趋势↑/↓ (价格抖动视为噪音), 否则 震荡~ (chop)。组合显示如「翻多▲ · 趋势↑」(强) /
  「翻空▼ · 震荡~」(谨慎)。`momentum_flip.py` 加 `compute_slope_regime` + `analyze` 合并;
  期权链标签按 flip+regime 富文本着色 (趋势绿/红, 震荡琥珀)。(`momentum_flip.py`, `widgets/option_chain.py`)
- **2026-06-26** — **期权链工具条加 ES 动量翻转指示 (Vordinkkk 法; 只看 ES)**。
  在「全部」与「刷新报价」之间显示 **ES** (E-mini S&P 500 连续期货) 的动量翻转: 翻多▲(绿)/翻空▼(红)/
  当前方向 多·空 / 无数据 —。算法移植自 `vordinkkk_momentum`: 1分钟K线, 动量=收盘[t]−收盘[t−10],
  翻转=动量穿越0 且 |导数|≥0.4 (`momentum_flip.py`)。新增引擎 `request_es_momentum_bars` (CONTFUT@CME
  1分钟 close 序列, 阻塞→后台线程); 期权链每 60s 后台拉取计算, 经信号回 GUI 更新。与当前期权品种无关,
  始终只观测 ES。无 CME 期货行情权限则显示「—」。(`momentum_flip.py`, `ibkr_engine.py`, `paper_engine.py`,
  `widgets/option_chain.py`)
- **2026-06-26** — **期权链「刷新报价」左侧显示今日交易统计: 笔数 / 胜率 / 盈亏比 (不列明细)**。
  新增 `trade_stats.py` 的 `TradeStats` —— 按「持平→再持平」一个完整回合 (开+平算1笔) 做回合制 FIFO
  统计: **笔数**=已平仓回合数, **胜率**=盈利笔数/总笔数, **盈亏比**=平均盈利/平均亏损 (无亏损→显示 ∞)。
  由成交流喂入: 真实引擎在 `execDetails` 喂 (含 `reqExecutions` 当日**历史回放** → 覆盖「今日全部含重启前」,
  按 execId 去重防双计, 每日随盈亏一起重置); 模拟引擎在 `_update_position` 喂 (本次运行累计)。佣金用估算
  (够分类盈亏)。两引擎各一套, 经 `trade_stats_updated` 信号 + 主窗口按**当前活动引擎**过滤后, 推到期权链
  `set_trade_stats` (胜率<50%/盈亏比<1 标红, 否则绿)。(`trade_stats.py`, `ibkr_engine.py`, `paper_engine.py`,
  `widgets/option_chain.py`, `main_window.py`)
- **2026-06-26** — **修: error 321 "Invalid account code" —— 用 managedAccounts 取权威账户**。
  reqPnL/reqPnLSingle 之前只从 `accountSummary` 取账户名, 多账户/账户组时会被最后一行覆盖成
  reqPnL **不接受**的代码 → 321 刷状态栏。改为实现 EWrapper 的 **`managedAccounts`** 回调(连接即
  自动下发**有效账户代码**列表, 早于 accountSummary), 取第一个为主账户; `accountSummary` 仅在其
  未给出时兜底。另加 321 专门处理: 记下实际所用账户(排查用)并一次性提示, 不再刷屏。
  (`ibkr_engine.py`)
- **2026-06-26** — **修: 某些到期日整张表空(如 NVDA 07/10)—— 期权链按到期日取真实合约**。
  根因(日志实锤): `NVDA 260708 C 210` 成交而 `NVDA 260710 C 210` 报 **code=200 No security definition**
  —— `request_option_chain` 把所有 option class 的到期日与行权价**并集**化, `reqSecDefOptParams` 的行权价
  又是「该 class 跨所有到期日的并集」, 于是某到期日会被配上它**实际不存在**的行权价/class → reqMktData/下单
  全 200 → 整张到期日表空。修复分两层:
  ① `request_option_chain` 改为**按到期日**从「实际列出它的 class」解析行权价+tradingClass(SPX/SPXW 等多 class 仍正确);
  ② 新增 `request_option_strikes_live`(**reqContractDetails** 按到期日取**真实存在**的合约), 期权链建表时**懒加载**该
  到期日真实行权价(后台线程 + 信号回 GUI, 显示「加载中」占位; 确无合约则提示「该到期日无可用合约」, 不再拼假行)。
  权威 tradingClass 写回缓存 → 下单/点价梯也用对的 class, 不再 200。`paper_engine` 同步代理两个新方法。
  (`ibkr_engine.py`, `widgets/option_chain.py`, `paper_engine.py`)
- **2026-06-26** — **计算器右下角指数条加第二行: 美债收益率 13周/5年/10年(刷新 30s)**。
  第一行仍是 SPY/SPX(+换算)/VIX; 新增第二行经 CBOE 收益率指数订阅: **IRX(13周)/FVX(5年)/TNX(10年)**,
  显示百分比(TNX/FVX 指数=收益率×10 → `scale=0.1` 还原; IRX≈收益率)。**无标准 2 年期 CBOE 指数**,
  按用户选择用 **13周(IRX)** 作短端替代。利率变动慢 → 独立 30s 定时器刷新(连接后先排 3s/8s 两次快更出值)。
  `config.INDEX_SYMBOLS` 加入 IRX/FVX/TNX/TYX(→ `_make_underlying_contract` 按 CBOE/IND 建约)。
  注: 需 CBOE 指数行情权限, 无则显示「—」; 利率行常驻 3 条行情线(Gateway 行情线紧张时可改 30s 快照)。
  (`widgets/option_calculator.py`, `config.py`)
- **2026-06-26** — **修: 期权链切到/切换到期日时自动把最接近现价的行权价居中(不再停在最低档如 0.5)**。
  根因: `scrollToItem` 在刚建/刚切的表上执行时 viewport 高度还是 0 → 居中无效, 表停在最上面(最低行权价)。
  改为**延后一轮事件循环**(`QTimer.singleShot(0, _center_and_snapshot)`)等布局完成再滚, 并用**最新现价**
  每次重算 ATM 行(`_recompute_atm_row`); 居中后才对可见行拉快照, 平值附近优先出价。(`widgets/option_chain.py`)
- **2026-06-26** — **点价梯滚轮可够到任意挂单价 + 期权链显示全部行权价(按视口取价)+ 切标的不再卡顿 + 计算器下方大盘指数条**。
  ① **点价梯边缘自动扩展**: 滚轮滚到顶/底边缘时向该方向追加 `LADDER_EXTEND_CHUNK` 档(只增不重建,
  现价始终在范围内 → 不触发 `_refresh` 自动重建、不闪烁), 故现价 1.3 也能滚到 5 挂限价; 顶部插入后
  复位滚动条保持视野; 上限 `LADDER_MAX_ROWS=1600`。新增 config `LADDER_ROW_HEIGHT/_EXTEND_CHUNK/_MAX_ROWS`。
  ② **期权链显示全部行权价**: `load_chain` 不再按 ATM±N 裁剪(原 10/15 档 → 够不到 SPY 695 put);
  改显示整条链, 行情线占用改由「**按视口取价**」控制 —— 表格行**懒加载**(切到该到期日 Tab 才建行),
  快照只对**当前滚动可见行**拉取(去抖 250ms), 切 Tab 先滚到 ATM 居中。解决「远端滚不到 / 多到期日空表」。
  ③ **切标的不再卡顿(pythonw 灰屏)**: 根因是 `price_ladder.set_option` 在 GUI 线程做 IBKR socket
  订阅/退订(Gateway 繁忙时 `socket.send` 阻塞数秒)。改到**后台线程**(代数计数器保证只留最近一次切换的
  订阅、快速连切不泄漏行情线); 叠加期权链懒加载, 切标的瞬间完成。
  ④ **计算器下方大盘指数条**: SPY/SPX 现价 + 换算(SPX≈SPY×10)在左, VIX(按水平着色)在右,
  经 `subscribe_stock_tick` 订阅(SPX/VIX 为 IND 合约), 无 CBOE 指数权限时显示「—」。
  (`widgets/price_ladder.py`, `widgets/option_chain.py`, `widgets/option_calculator.py`, `config.py`)
- **2026-06-25** — **新增宏观行情监控 `macro_monitor.py`(独立程序, clientId=13, 只读)**。监控
  **美债各期限利率**(CBOE 收益率指数 IRX 13周/FVX 5年/TNX 10年/TYX 30年; TNX/FVX/TYX 指数=收益率×10
  故 `scale=0.1` 还原)+ **原油/黄金/白银**(连续期货 CONTFUT: CL/GC/SI), 每项显示**当前价 +
  1月/3月/6月/1年区间最高/最低**(由日线历史本地算, **不画曲线**)。复用 `IBKREngine` 连接(独立
  clientId=13, 跟随 USE_GATEWAY 选端口), 自定义 IND/CONTFUT 合约直接驱动 `engine._app` 的
  `reqMktData`(现价, 读 `_tick_data`)与 `reqHistoricalData`(`_hist_data` 阻塞取数, 窗口取末尾
  21/63/126/252 根日线)。**默认连实盘端口**取真实行情(只读不下单; 模拟盘常缺期货/指数行情)。
  新增 `config.IBKR_MACRO_CLIENT_ID=13`、`start_macro.bat`/`start_macro_gateway.bat`。
  注: 指数/期货行情需相应权限, 无则显示「—」; 收益率口径不同可改 `INSTRUMENTS` 的 `scale`。
  (`macro_monitor.py`, `config.py`, `start_macro*.bat`)
- **2026-06-25** — **减轻 Gateway 压力: 账户摘要/各币种现金订阅改幂等(消除每 3 秒 cancel+重订 churn)**。
  根因(看 Gateway 日志):`request_account_summary` 与 `request_currency_balances` 每次调用都
  `cancelAccountSummary + reqAccountSummary` 重订, 而 `account_bar` 每 3 秒调一次 → Gateway 上
  "EMsgPacer 不停发请求/发取消、NonAwtClientQueue 任务堆积(tasks 138)"。但 `reqAccountSummary` 是
  **流式订阅**(订一次持续推送, 约每 3 分钟或值变化时), 无需重订。改为**幂等**(已订阅则直接返回,
  与既有 `request_pnl` 一致; 重连新建 `IBKRApp` → req_id 复位 → 自动重订)。净值/现金更新节奏改由
  IBKR 流式推送(略慢于原来的 3 秒强刷), 但未实现/今日盈亏仍由 `reqPnL` 流持续驱动, 不受影响。
  (`ibkr_engine.py`)
- **2026-06-25** — **重启后自动加载当日历史委托(已成交/已撤销), 不只当前挂单**。连接时除
  `reqOpenOrders` 外加 `reqCompletedOrders(False)`;新增 `completedOrder`/`completedOrdersEnd` 回调,
  经新助手 `_option_from_contract`(期权/正股/期货/组合)复用 `open_order_received` → `_on_open_order`
  进委托面板(按 `orderState.status` 映射 已成交/已撤销),跨会话完成单 `orderId` 为 0 时用 `permId` 兜底主键。
  注:IBKR 仅提供「上次服务器重置以来」(约当日)的完成单, 无法取更久历史; 本地模拟模式无 IBKR 历史。
  (`ibkr_engine.py`)
- **2026-06-25** — **全局崩溃捕获: 交易时不再静默闪退, 崩溃必落日志**。新增 `crash_handler.py`,
  入口(`main.py`/`main_gw.py`)在日志重定向后 `install_crash_handler(sys.stderr)` 安装:
  ① `sys.excepthook` —— PyQt 槽函数抛异常时**记录完整 traceback + 非模态弹窗提示, 事件循环继续**
  (默认 PyQt5 会直接 abort 整个进程 = 闪退, 这是"交易中突然消失"的主因之一);
  ② `threading.excepthook` —— 工作线程(连接/加载/reader)异常落日志;
  ③ `faulthandler` + Qt `qInstallMessageHandler` —— C 层 segfault/abort、Qt `QtFatal` 也 dump 栈到日志。
  以后任何崩溃都能在 `logs/app[_gw]_*.log` 找到现场。(`crash_handler.py`, `main.py`, `main_gw.py`)
- **2026-06-25** — **条件单两种用法 + 期货用「点数」表示 + 期货开多强制带止盈止损**。
  ① **两种条件单**: a)「挂条件单」按钮对**当前持仓**挂(原有, 按面板「数量」); b) 新增点价梯
  「**随买入单附带**」勾选框 —— 用买入/市价买入开仓时**按买入数量**自动附带同样的止盈/止损。
  ② **期货用点数**: 期货合约时条件单输入框语义切到「止盈 +点 / 止损 −点」(相对入场价),
  附带到买入时入场价=**成交价**(故期货市价/限价都等成交回报后才挂)、对持仓挂时入场价=**持仓均价**;
  如买入 7140、止盈 100/止损 50 → 实际 7240 止盈、7090 止损(`_trigger_price` 换算, README 示例)。
  ③ **期货开多强制带止盈+止损**(`config.FUTURES_REQUIRE_BRACKET`, 默认 True): 期货 BUY 前
  `_resolve_buy_bracket` 校验, 未设好则弹窗拦截 + 自动展开条件单面板; 期权/正股仅在勾「随买入单附带」时附带。
  限价单经 `_pending_buy_brackets` 等成交回报 `_on_exec_arm_bracket`(两引擎都监听)再挂, 避免未成交时误触发。
  新增点价梯 `attach_to_buy()`/`get_bracket(require_both)`(带 `by_points`)/`open_cond_panel()`/
  `_sync_cond_input_mode()`; 主窗口 `_trigger_price`/`_resolve_buy_bracket`/`_arm_buy_bracket`。
  (`config.py`, `widgets/price_ladder.py`, `main_window.py`)
- **2026-06-25** — **计算器右列加「标的价→期权价」方向(双向 what-if)**。右列顶部加单选切换:
  原「期权价→标的价」(反解所需标的价)保留;新增「标的价→期权价」—— 输入**假设的正股/指数价格**,
  用 Black-Scholes 正算该价位下的期权价,并与当前盘口中间价比对(相对涨跌 ↑绿/↓红, 到期取内在价值)。
  两方向共享 K/IV/r/到期参数;播种时同时填目标期权价(盘口中价)与假设标的价(当前标的)。
  `_solve` 改为按模式分派 `_solve_underlying`/`_solve_price`,输出标签改为通用三行随模式改标题。
  (`widgets/option_calculator.py`)
- **2026-06-25** — **修点价梯闪烁(单边报价时无限重建)**。根因:`_rebuild_ladder`(建梯)与 `_refresh`
  (判定是否需重建)的**居中价算法不一致** —— 只有 bid 或只有 ask 时(SPY 0DTE 常见,行情线吃紧时
  尤甚),`_refresh` 用「存在的那一侧」判定价格已出范围要求重建,而 `_rebuild_ladder` 用 `OptionInfo.mid`
  (单边时退化为 `last`)居中,中心对不上 → 下一拍又判定出范围 → 每 200ms 重建 201 行 = 闪烁。
  修复:抽出共用的 `_center_price()`(bid&ask 都在取中值, 否则取存在的一侧, 再退 `last`),建梯与重建判定
  统一调用。(`widgets/price_ladder.py`)
- **2026-06-25** — **多腿组合并入主 GUI 作独立 Tab + 账户条「换汇」改为美东实时时钟**。
  ① 原「策略组合」弹窗按钮改为主窗口中央**顶层 Tab**:「单腿点价」(现有点价梯/期权链/持仓委托)与
  「多腿组合」(策略生成器)互不干扰。`strategy_window.py` 把核心重构成可嵌入的 `StrategyPanel(QWidget)`
  (新增 `set_engine`/`set_symbol`/`ensure_loaded`/`load_chain`,**懒加载**:首次切到该 Tab 才拉期权链,
  不浪费行情线),保留 `StrategyWindow(QMainWindow)` 薄壳以兼容独立窗口用法;主窗口用 `QTabWidget` 承载、
  跟随连接/模式热切换/标的变化更新引擎与标的,关窗时 `cleanup()`。删除主窗口 `_on_open_strategy` 与顶栏按钮。
  ② **去除换汇**:`AccountBar` 移除「换汇」按钮与 `currency_exchange_clicked` 信号,改为右侧**美东时间时钟**
  (每秒刷新 `America/New_York`,无 tz 数据回退本地);主窗口删 `_on_currency_exchange` 及其连接,
  删除孤立的 `widgets/currency_dialog.py`(引擎 `place_forex_order` 后端保留,只是无 UI 入口)。
  各币种现金余额条 `CurrencyBalanceBar` 保留不变。
  (`widgets/strategy_window.py`, `main_window.py`, `widgets/account_bar.py`, `widgets/currency_dialog.py` 删)
- **2026-06-22** — **点价梯加「条件单」(止盈/止损)**。点价梯勾选框那行加「条件单 ▾」开关, 展开面板:
  ☑止盈 / ☑止损(可单选)+ 各自触发价 + 数量 + ☐用IBKR原生 + 「挂条件单」+ 已挂列表(✕ 取消)。
  **含义**:到达触发价才挂出对应**限价单**。**本地模式(默认)**到价前不发到 IBKR,由本程序每 0.5s 监控现价、
  到价才提交 —— **规避 IBKR「同合约不能双向挂单」(错误 201,正是"有卖单时挂不了买单"的根因)**,
  但**只在程序运行时监控**(已持久化到 `conditional_orders.json`,重连恢复;关程序/崩溃即失效, 面板有醒目提示)。
  **原生模式**:止损用 IBKR `STP LMT`(GTC,服务器端,关程序也有效)、止盈用普通 SELL LMT,但仍受 201 限制。
  新增 `conditional_orders.py`(`ConditionalOrderManager`:计时器监控 + 触发下单 + 订阅/退订行情 + json 持久化)、
  `models.ConditionalOrder`、`ibkr_engine.place_stop_limit_order`(secType 感知)/`_est_commission`;
  主窗口接管理器(按品种路由下单、触发提示音、断开保留/重连恢复)。当前面向「平多·SELL」。
  (`conditional_orders.py`, `models.py`, `ibkr_engine.py`, `paper_engine.py`, `widgets/price_ladder.py`,
  `main_window.py`, `.gitignore`)
- **2026-06-22** — **组合分析器「今日合并K线」升级为多周期/多天「组合K线」**。原蜡烛图 `duration` 写死
  `1 D`(仅当日);改为按「周期」下拉取 `CHART_TIMEFRAMES` 的 (bar_size, duration),1分/5分/1时/2时/4时/
  日线 等都能给出**多天**组合蜡烛(如 5分≈1周、1小时≈1月)。按钮改名「组合K线(蜡烛)」,状态/摘要文案去掉
  「今日」, 跨天时间标签带日期。x 轴时间刻度本就 `%m-%d %H:%M`、绘图通用, 无需改。依赖期权历史数据权限
  (无权限用「▶ 录制当日」实时累积)。(`combo_analyzer.py`)
- **2026-06-22** — **彻底删除独立正股 client**(功能已并入主 GUI「类型」切换)。删 `stock_trader.py`、
  `stock_trader_gw.py`、孤立图标 `stock_app.ico` / `stock_icon.png`、桌面「IBKR 正股交易」快捷方式;
  确认无其它模块依赖(仅 `stock_trader_gw` 引用 `stock_trader`)。README 目录树/快速开始/文件详解/日志小节同步清理。
  正股/期货交易统一走 `MainWindow` 伪合约(`right='STK'/'FUT'`)。(删 `stock_trader.py`, `stock_trader_gw.py`, 图标)
- **2026-06-22** — **组合分析器加 Gateway 版入口 + 删除正股启动 bat**。新增 `combo_analyzer_gw.py`
  (设 `IBKR_USE_GATEWAY=1`、独立文件名故不杀运行中的 `combo_analyzer.py`、日志写 `combo_app_gw_*.log`、
  标题加 [GW];`ComboAnalyzerWindow` 连接后的标题按 `USE_GATEWAY` 保持 [GW] 标记)与 `start_combo_gateway.bat`,
  与 `main_gw.py` 对称。删除 `start_stock.bat` / `start_stock_gateway.bat`(正股/期货已并入主 GUI「类型」切换,
  独立正股 client 的启动 bat 不再需要;`stock_trader.py` 文件本身保留)。(`combo_analyzer.py`,
  `combo_analyzer_gw.py`, `start_combo_gateway.bat`, 删 `start_stock*.bat`)
- **2026-06-22** — **修 2107/2108 误报标红**(久存待办)。`2107/2108`「farm inactive but should be
  available upon demand」是 IBKR 行情farm的**空闲待命**状态(取数据时自动重连),非故障;原 `_on_error`
  把它与真断开(2100/2103/2105)一样标红"行情数据连接异常"。改为:2104/2106=正常、**2107/2108=中性提示
  (不标红)**、仅 2100/2103/2105 标红。(`main_window.py`)
- **2026-06-22** — **Gateway 卡死(JTS死锁)时连不上 → 给明确提示让用户重启 Gateway**。日志实测
  `JTS-DeadlockMonitor` 死锁(`EWriter` + `usopt*` 期权行情farm线程)→ Gateway 无法再向 API 写数据、
  clientId 也不释放,客户端**无法自愈**,必须重启 Gateway。这非本程序 bug。改:连接超时(套接字通但
  `nextValidId` 不回)时 `connect()` 明确报"Gateway 无响应/可能卡死,请重启",并清空 `self._app`;
  326 全占用与 `main_window` 的连接/切换失败提示也都改为提示"重启 Gateway"。**根治靠升级 IB Gateway
  到最新稳定版**(JTS 死锁是版本 bug)+ 降低美股期权行情订阅负载。(`ibkr_engine.py`, `main_window.py`)
- **2026-06-22** — **XSP 最小跳动改 0.01**(原 0.05/0.10 太粗)。CBOE 实测 XSP(Mini-SPX)**全系列统一
  $0.01**(与 SPX 的 0.05/0.10 不同),`config.TICK_SIZE_OVERRIDES["XSP"]` 改为 `(0.01, 0.01)`,点价梯
  即按 0.01 排档。(`config.py`)
- **2026-06-22** — **修「模拟盘切回实盘连不上 / 刚连上又断开」(clientId 释放竞态)**。日志实测 326
  「clientId in use」:热切换时旧 clientId 尚未被 Gateway 释放(它正忙加载持仓时更慢)→ 退避到 +10 重试。
  ① **根治"刚连上又断"**:重试丢弃旧连接前先把 `self._app` 置空,使被丢弃连接的 reader 线程
  (`_run_wrapper`)判定 `app is self._app` 为 False、**不再误发 `disconnected`**(该信号原会晚于新连接的
  `connected` 到达 GUI → 看似连上又断)。② 释放等待加长:`reconnect` 1.5s→3s、326 重试间隔 0.5s→1.5s、
  重试次数 3→5(10/20/30/40/50)。③ 新增 `_base_client_id`:每次连接都从**标准 id**(10)起算重试,
  避免一次 326 退避后 `_client_id` 永久漂到 20/30。(`ibkr_engine.py`)
- **2026-06-22** — **加「延迟行情自动回退」(修模拟盘看不到期货报价)**。根因:IBKR 行情订阅绑在实盘账户,
  **模拟盘默认无行情**(未开"与模拟账户共享行情"),且账户未订阅期货行情时点价梯会空白、看似"搜不到"。
  改:某合约报 **354 / 10168「未订阅实时行情」**时,引擎一次性 `reqMarketDataType(3)` 切**延迟行情**并按原 reqId
  重订当前所有行情线(已订阅实时的合约仍走实时);`IBKRApp` 加 `_tick_req_contract`(reqId→合约)与
  `_switch_to_delayed_and_resubscribe`。正解仍是去 IBKR Client Portal 给模拟盘**开启行情共享 / 订阅期货行情**。
  (`ibkr_engine.py`)
- **2026-06-22** — **修「期货搜不到」**:期货模式下搜索框改用**内置期货列表本地补全**(IBKR
  `reqMatchingSymbols`/`symbolSamples` 不返回期货根代码,之前结果被过滤成只剩 STK/IND/ETF → 期货永远搜不到);
  切到「期货」且当前标的非期货根代码时**自动填默认 `ES`** 并加载,免去"SPY 不在列表"的困惑。
  注:期货**实时盘口**需账户有 CME 等期货行情订阅,无订阅时合约能解析、能下单,但点价梯无报价。
  (`widgets/symbol_bar.py`, `main_window.py`)
- **2026-06-22** — **期权 GUI 顶栏加「类型」三选一(期权/正股/期货),默认期权,可切正股/期货交易**。
  在 `SymbolBar` 顶栏最左加 `类型` 下拉(`期权`默认/`正股`/`期货`)+ 期货专用「合约月份」下拉
  (新信号 `instrument_changed`/`future_expiry_changed`,方法 `set_instrument`/`populate_future_expiries`)。
  **复用现有点价梯/持仓/委托面板**(沿用 `stock_trader.py` 的伪合约思路):切到正股 → 隐藏期权链、
  用 `OptionInfo(right='STK')` 载入点价梯;切到期货 → 后台 `resolve_futures_contracts` 解析**近月 + 之后
  几个季月**(下拉可选),用 `OptionInfo(right='FUT')` 载入。下单按品种路由:期权走 `place_limit/market_order`、
  正股走 `place_stock_order`、期货走新增 `place_futures_order`;平仓对正股/期货用市价反向单(数量取
  `reqPositions`)。新增 `config.FUTURES_SPECS`(ES/MES/NQ/MNQ/RTY/M2K/YM/MYM/CL/MCL/GC/MGC 的交易所/乘数/
  tick)+ `FUTURES_COMMISSION_*`;`models` 的 `OptionInfo` 支持 `right='FUT'`(`__fut__SYM_YYYYMM` 键、
  `(期货 YYMM)` 显示)、`PortfolioPosition` 加 `FUT→期货`;`ibkr_engine._on_portfolio_position` 把正股/期货
  持仓也缓存进 `_ibkr_positions`(伪合约键),使点价梯持仓数量/平仓按钮在新品种下也工作(顺带修好
  `stock_trader.py` 点价梯平仓按钮);点价梯 tick 抽成 `_tick_sizes()`、确认框数量单位按品种(张/股/手);
  持仓面板加「期货」筛选 + `set_filter()`。`PaperEngine` 补 `place_stock_order`/`place_futures_order`(本地撮合,
  盈亏为近似)使本地模拟也能测。(`config.py`, `models.py`, `ibkr_engine.py`, `paper_engine.py`,
  `main_window.py`, `widgets/symbol_bar.py`, `widgets/price_ladder.py`, `widgets/position_panel.py`)
- **2026-06-19** — **双击持仓/委托的合约 → 跳到该标的 + 加载到点价梯**。委托面板 `order_panel` 新增
  `option_selected` 信号(双击行,COMBO 跳过)+ `_row_options` 行映射;`_on_option_selected` 增加:若合约属于
  另一标的则切换标的并重载期权链(`symbol_bar.set_symbol`),再把合约载入点价梯+计算器(持仓面板原本就有双击跳转)。
  (`widgets/order_panel.py`, `widgets/symbol_bar.py`, `main_window.py`)
- **2026-06-19** — **实盘↔模拟切换时盈亏/净值显示按各自账户独立**。`account_bar.stop()`(断开/切换时调用)把
  今日盈亏/未实现/总资产/现金/购买力清回「--」,避免残留上一个账户的数字;重连后由新账户(模拟连 4002 → 模拟账户,
  实盘连 4001 → 实盘账户)的 reqPnL/reqPnLSingle 重新填充,数据本就按连接的账户独立。(`widgets/account_bar.py`)
- **2026-06-19** — **修复热切换后模式下拉/标的框卡死**。切换模式时 `set_switching(True)` 禁用了下拉+标的输入框,
  重连完成后从未复位 → 切到模拟/实盘后无法再改标的、无法再切回(实盘新连正常因为没走切换)。修复:
  `_on_connected` / `_on_disconnected` 开头调 `set_switching(False)` 复位。(`main_window.py`)
- **2026-06-18** — **今日盈亏修好: dailyPnL 不可用时用 已实现+未实现 兜底**。部分账户的 reqPnL
  `dailyPnL` 会返回 DBL_MAX(不可用)并被转为 NaN,但同一回调的 `unrealizedPnL` / `realizedPnL`
  仍有效。`update_daily_pnl` 在 dailyPnL 为 NaN 时取 `realizedPnL + unrealizedPnL`,
  dailyPnL 可用时仍优先用它。
  (`ibkr_engine.py`, `widgets/account_bar.py`)
- **2026-06-18** — **真正成交播放提示音**。新增 `sound_alerts.play_fill(side)`(后台线程播放,不卡 GUI):
  优先放 `sounds/BUY.(wav|mp3)` / `sounds/SELL.(wav|mp3)`(可用 GPT-SoVITS 生成),回退 `sounds/FILL.*`,
  再回退 winsound 蜂鸣(买升调/卖降调)。期权 GUI 与正股 client 均接 `execution_received`(仅真实引擎成交,
  本地模拟不响)。语音文件放 `sounds/` 即生效,无需改代码。**已用训练好的御坂美琴 misaka v3 模型生成
  `sounds/BUY.wav`(日语「買い」)/`SELL.wav`(日语「売り」)**。(`sound_alerts.py`, `sounds/`, `main_window.py`, `stock_trader.py`)
- **2026-06-18** — **修今日/未实现盈亏闪 0 + 重连后盈亏归 0**。① `request_pnl` 改**幂等**(reqPnL 是流式
  订阅,`account_summary_end` 每 3 秒会调它,原来每次 cancel+重订 → 重订瞬间初值不稳 → 闪 0;现已订阅则直接
  返回)。② **未实现盈亏单一来源**:reqPnL 流接管后,账户摘要里每 3 秒推来的 `UnrealizedPnL`(常为 0/陈旧)
  不再覆盖(`_unrealized_from_stream` 标志;断开时重置以便重连后再作初始回退)。③ 持仓面板 API 期权持仓在
  `reqPnLSingle` 到达前显示「--」而非误导性的 $0.00(新增 `has_pnl` 行标志)。(`ibkr_engine.py`,
  `widgets/account_bar.py`, `widgets/position_panel.py`)
- **2026-06-18** — **持仓一律以 IBKR API 为准 + 撤单类无害响应不再误报**(修"撤单弹拒单框 / 凭空多一个持仓 / 数目不对")。
  ① 错误 **161 / 10147 / 10148**(撤单时订单已不可撤或找不到 —— 多半已成交/已撤)在 `error()` 里静默返回:
  不弹框、不标 ERROR、不写拒单日志(以前会把刚成交的单误显示成"已拒绝")。② **真实引擎 `positions` 属性
  改为返回空**,`_on_execution` 不再本地累加持仓 —— 持仓唯一真相来自 `reqPositions`(`_ibkr_positions`):
  持仓面板用 `portfolio_position_received` + `reqPnLSingle` 渲染(不依赖逐合约行情,Gateway 快照模式也准),
  点价梯摘要改用新方法 `get_position()`,`get_position_qty()` 只读 API。平仓后 reqPositions 推 0 → 自动消失、
  不留残影,也不再有幻影持仓/数目错。模拟引擎 `PaperEngine` 仍用本地撮合持仓(无 API 可用)。
  (`ibkr_engine.py`, `paper_engine.py`, `widgets/price_ladder.py`)
- **2026-06-18** — **期权链报价改一次性快照 + 新增「🔄 刷新报价」按钮(修 Gateway 下整条链/TSLA 无数据)**。
  根因:Gateway 行情线收紧到 45,期权链原本对每个行权价持续流式订阅 → 线不够 → 全 "—"(TSLA 尤甚)。
  改为:引擎新增 `snapshot_option_tick`(`reqMktData snapshot=True`,IBKR 推一次 bid/ask/last/vol 后自动
  取消,**不占常驻线**)+ `tickSnapshotEnd` 清理映射;期权链切 Tab / 点按钮各拉一次快照,显示定时器只读
  缓存不发请求(无持续压力)。顺带:错误 **300**(cancelMktData 找不到 tickerId,快照自动取消后常见)
  改为静默 + 清理,不再刷状态栏。`PaperEngine` 加 `snapshot_option_tick` 委托。
  (`ibkr_engine.py`, `paper_engine.py`, `widgets/option_chain.py`)
- **2026-06-18** — **正股 client 补上 Gateway 版入口 + 两个桌面快捷方式改指 Gateway 新版**。新增
  `stock_trader_gw.py`(设 `IBKR_USE_GATEWAY=1`、独立文件名故不杀运行中的 `stock_trader.py`、日志写
  `stock_app_gw_*.log`、标题加 [GW])与 `start_stock_gateway.bat`,与 `main_gw.py` 对称。桌面
  「IBKR 点价交易」→ `main_gw.py`、「IBKR 正股交易」→ `stock_trader_gw.py`(图标/工作目录不变)。
  旧 TWS 版入口与 bat 全部保留。(`stock_trader_gw.py`, `start_stock_gateway.bat`, 桌面 .lnk)
- **2026-06-18** — **期权 GUI + 正股 client 都新增「各币种现金余额」显示**。引擎用 `reqAccountSummary
  "$LEDGER:ALL"` 拿到每个币种的 `CashBalance`(新信号 `currency_balance_updated`/`currency_balances_end`、
  方法 `request/cancel_currency_balances`,独立于普通账户摘要订阅);新增复用控件
  `widgets/currency_balance.py`(`CurrencyBalanceBar`,显示 `币种: EUR €414.00  USD $0.00`,非零排前、
  含 0 也显示)。期权 GUI 内嵌进 `AccountBar`(随 3s 刷新),正股 client 放顶栏(连接时请求);
  `PaperEngine` 给出模拟 USD 余额以保持接口一致。直接定位"有欧元没美元"导致买美元期权被拒的根因。
  (`ibkr_engine.py`, `paper_engine.py`, `widgets/currency_balance.py`, `widgets/account_bar.py`,
  `main_window.py`, `stock_trader.py`)
- **2026-06-18** — **新增 Gateway 版入口 `main_gw.py` + `start_gateway.bat`(解决 TWS 频繁崩溃)**。
  根因诊断:TWS 是重型 Java GUI,期权 GUI(10)+正股 GUI(11)各订 ~62 行行情、合计超 ~100 行账户
  上限,叠加界面渲染易卡死/崩溃。新版改动:① `config.py` 加 `USE_GATEWAY`(环境变量
  `IBKR_USE_GATEWAY=1`)开关,开则连 IB Gateway(4001 live / 4002 paper)且收紧订阅
  (`MAX_SIMULTANEOUS_STREAMS` 95→45, `CHAIN_STRIKES_AROUND_ATM` 15→10,两个 GUI 合计 <100 行);
  ② `ibkr_engine.py` 端口选择按 `USE_GATEWAY` 路由;③ `option_chain.py` 用 `CHAIN_STRIKES_AROUND_ATM`;
  ④ 新入口 `main_gw.py` 文件名独立 → `kill_previous_instances` **不会杀运行中的 `main.py`(旧版)**,
  新旧可并存对比;日志写 `app_gw_*.log`,窗口标题加 `[GW 新版]`。**旧版完全不受影响**
  (`start.bat`/`main.py` 不设环境变量 → TWS + 95/15,字节级行为不变)。
  **顺带修 bug**:`config.py` 的 GW 端口常量原先写反(paper=4001/live=4002),已按 IB 默认改正
  (live=4001/paper=4002);README §7 自相矛盾的端口说明一并修正。
  (`config.py`, `ibkr_engine.py`, `combo_analyzer.py`, `widgets/option_chain.py`, `main_gw.py`,
  `start_gateway.bat`)
- **2026-06-18** — 组合分析器加**连接模式下拉**(IBKR模拟盘 7497 默认 / 实盘 7496):此前写死 LIVE
  会直连实盘真钱;改为默认连模拟盘, 可端到端测试组合下单而不动真钱, 选实盘弹确认。(`combo_analyzer.py`)
- **2026-06-18** — **新增「IBKR模拟盘」模式,可连模拟账户真实测试下单**。原「Paper」是本地撮合、不发单;
  新增第三模式 `TradingMode.IBKR_PAPER`,走真实 `IBKREngine` 但连 7497 端口,把订单真实提交到 IBKR
  模拟账户,端到端验证 placeOrder→TWS→成交回报。顶栏模式下拉改三项(本地模拟/IBKR模拟盘/实盘,
  item data 存 `TradingMode.value`);端口/引擎判定用新属性 `is_live_port`/`uses_ibkr_engine`;
  状态栏统一用 `mode.label`。(`models.py`, `ibkr_engine.py`, `main_window.py`, `widgets/symbol_bar.py`)
- **2026-06-18** — 组合分析器加「**今日合并K线**」按钮:拉各腿当日 OHLC 合并成组合蜡烛图
  (`compute_combo_ohlc`, 空头腿反向贡献高/低), 复用 `CandlestickItem`, 可滚轮缩放/拖动;
  绘图统一改为索引 x 轴 + 自定义时间刻度(折线与蜡烛共用)。(`combo_analyzer.py`, `widgets/combo_pricing.py`)
- **2026-06-18** — 组合分析器:加载链/选好腿后, 腿表「最新价」与组合净价**持续实时刷新**
  (自动订阅各腿行情, 不必先点「计算/录制」), 修复「加载链后表格看起来空着」的困惑。(`combo_analyzer.py`)
- **2026-06-18** — 新增诊断脚本 `check_option_history.py`(clientId=99, 只读):实测账户是否有
  「期权历史数据」权限(请求 SPY 当日 1 分钟 bar, 试 TRADES/MIDPOINT, 打印根数或错码)。(`check_option_history.py`)
- **2026-06-18** — 组合分析器加**当日实时录制**:每 2 秒用各腿实时盘口中价合成组合净价累积成当日
  曲线, 不依赖历史行情权限 (只需实时行情), 适合「只看当日」。(`combo_analyzer.py`)
- **2026-06-18** — **期权计算器改两列 + 新增反向求标的价**。右列为 what-if 求解器:可改 K/IV/r/到期天数与
  **目标期权价**,用 BS 价对 S 单调的性质做括弧二分(`solve_underlying_for_price`)反推「期权要值目标价时
  标的需到的价位」,并对比当前标的显示需变动金额/%。换合约自动以实时值播种一次,「↺ 用实时值填充」可重置;
  Put 目标价超上界显示「无解」。左列保持原实时正向理论价不变。(`widgets/option_calculator.py`, `README.md`)
- **2026-06-18** — **修复持仓数量莫名变成 -1(幻影空头)**。本程序**只做多期权、从不做空**,但真实引擎
  `_on_execution` 有两个漏洞会建出负持仓:① 卖出超量/重复成交时 `new_qty == 0` 判断让数量穿到负数仍保留;
  ② 卖出一个本地未跟踪的合约(如 `reqPositions` 尚未回来的竞态)会在 `else` 分支直接建出 `-qty`。
  改为:`new_qty <= 0` 一律删除持仓;未跟踪键仅在 `fill_qty > 0`(买入/加仓)时才建仓,卖出不建幻影。
  与上一条的 `_ibkr_positions` seed 合起来,卖出旧持仓会正确归零而非变 -1。(`ibkr_engine.py`)
- **2026-06-18** — **修复重大 bug:崩溃/重启后上一会话的期权持仓卖不掉**。真实模式下 `IBKREngine._positions`
  仅由**本会话成交**填充,而崩溃前开的仓位重启后只经 `reqPositions()` 进到面板的 `_portfolio_positions`,
  引擎并不知道 → 「平仓」按钮报“无持仓可平”、面板里这类持仓双击也打不开点价梯(`option=None`)、
  即便卖出 `_on_execution` 还会建出幻影空头(-qty)。修复:引擎新增 `_ibkr_positions` 缓存 `reqPositions`
  的真实期权持仓;`close_position`/`get_position_qty` 回退到该缓存;`_on_execution` 首次成交前先以真实
  持仓做基线 seed(避免幻影空头);持仓面板为 OPT 组合持仓构造 `OptionInfo` 使双击可开梯平仓。
  (`ibkr_engine.py`, `widgets/position_panel.py`)
- **2026-06-18** — **新增期权组合分析器** `combo_analyzer.py`(独立程序, clientId=12):多腿组合
  (蝶式/铁鹰/跨式/垂直/日历...)即时净价 + 由各腿历史价合成**组合历史价曲线**(券商不提供);
  并支持 IBKR 原生 BAG **组合原子交易** —— 持仓作为整体,只能整组平仓/加仓,**不可单腿**,
  分组本地持久化(`combo_positions.json`)。新增引擎方法 `request_option_historical_data`、
  纯逻辑 `widgets/combo_pricing.py`、`start_combo.bat`、config `IBKR_COMBO_CLIENT_ID=12`。
  (`combo_analyzer.py`, `widgets/combo_pricing.py`, `ibkr_engine.py`, `config.py`, `start_combo.bat`)
- **2026-06-18** — **计算器理论价更新提速**:标的价 S 改为优先读高频的 `__stock__SYM` tick
  (随每笔成交跳动),模型 `undPrice`(每几秒才更新)仅作回退;刷新间隔 700ms→300ms。
  解决理论价随标的变动反应迟钝的问题。(`widgets/option_calculator.py`, `config.py`)
- **2026-06-18** — **模拟模式今日盈亏/未实现盈亏/净值计入手续费**:`_calc_unrealized_pnl` 改用 `net_pnl`
  (= 毛利 − 未平仓持仓累计佣金),修复模拟模式今日盈亏漏算开仓手续费的问题。~~真实模式不变
  (IBKR 的 dailyPnL 已含佣金...)~~ **← 此结论后被实盘实测推翻:reqPnL dailyPnL 未含手续费,
  见上方今日盈亏扣费那条。**
  (`paper_engine.py`)
- **2026-06-18** — **窗口自由缩放 + 子面板按比例联动 + 尺寸记忆**:三层 splitter 加 `setStretchFactor`
  (主竖向 1:1、下方横向 4:5、右侧竖向 5:2) 使面板随窗口等比联动;`setChildrenCollapsible(False)`
  防误折叠;最小尺寸降到 900×600;用 `QSettings` 持久化窗口几何 + 各 splitter 位置 (跨会话);
  点价梯分离/贴回后重设 stretch。(`main_window.py`)
- **2026-06-18** — 修复**未实现/今日盈亏偶尔闪烁成 0** 的 bug:IBKR 对尚未算出的 PnL 字段推 DBL_MAX
  (~1.8e308),原先 `pnlSingle._clean` 将其转成 0 再覆盖显示,导致好值被刷成 0。改为转 NaN,
  GUI(持仓面板 + 账户栏)跳过 NaN 字段、保留上一次的好值;账户级 `pnl()` 同步加 DBL_MAX 处理。
  (`ibkr_engine.py`, `widgets/position_panel.py`, `widgets/account_bar.py`)
- **2026-06-18** — 新增**期权理论价计算器**(主窗口右下角):Black-Scholes 用实时 IV + 标的价算「应有价格」
  并与盘口中间价比对(偏贵/偏便宜),随实时行情与时间衰减自动刷新,支持手动 what-if。
  引擎新增 `tickOptionComputation` 回调接收 IV/Greeks/标的价,`subscribe_option_tick` 对期权请求 generic tick 106。
  (`widgets/option_calculator.py` 新增, `ibkr_engine.py`, `config.py`, `main_window.py`)
- **2026-06-18** — 确立 `README.md` 为本目录唯一总体文档(活文档),加入维护约定与本变更记录。(`README.md`)
- **2026-06-18** — 分析行情连接码 **2108 / 2107**「inactive but should be available upon demand」误报问题:
  这是 IBKR 正常的**空闲(按需自动重连)**状态,非真故障,但 `main_window.py:_on_error` 当前把它和真正断开
  (2103/2105/2100)一样标红「行情数据连接异常」。**修复待定**:应把 2107/2108 单独归为中性提示、不标红,
  仅 2100/2103/2105 标红。(`main_window.py:838-850`, `config.py:92-101`)

### 已知问题 / 待办

- [x] **2108/2107 误报标红** —— 已修 (2026-06-22, 见变更记录): 2107/2108 改为中性提示、不标红。
- [ ] **期货 K 线图未支持**:`ChartWindow` 用 `_make_underlying_contract`(STK)取历史,期货根代码(ES 等)
  按正股取会无数据/报错;期货模式下「K线图」暂不可用,待后续让图表按 FUT 合约取历史。
- [ ] **本地模拟下正股/期货盈亏为近似**:`PaperEngine` 持仓盈亏按期权乘数(×100)估算,正股/期货数值不准,
  仅用于下单链路测试;真实/IBKR模拟盘下持仓盈亏来自 IBKR API,准确。
- [ ] **银 (SI) 等 tick<0.01 的期货未收录**:点价梯按 2 位小数网格,sub-cent tick 会错位,暂不收录。

---

> 历史里程碑(本变更记录建立前,摘自提交历史与 `../CLAUDE.md`):
> 实时显示标的价于期权链头 · 重连恢复挂单/撤单修复 · tick size 锁定 penny-pilot(0.01/0.05)并恒显买卖盘 ·
> 免确认快速下单 · 拒单 eTradeOnly/firmQuoteOnly 空字段修复 · 拒单日志 `order_rejects_*.jsonl` · 正股独立 client。
