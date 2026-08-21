"""期权当日 1 分钟 K 线窗口 — 期权链**双击**某合约打开。

轻量版图表 (与 ChartWindow 的正股全功能图分开):
- 数据: `IBKREngine.request_option_historical_data` (1 min / 1 D, 阻塞调用放工作线程);
- 数据源可切 成交价(TRADES) / 中间价(MIDPOINT) — 期权成交稀疏时中间价更连续;
- 自动刷新 (默认开, 10s 轮询重拉) — 期权历史数据不支持 keepUpToDate 流式的行情线占用,
  轮询即可满足"今日盘中"观察;
- 蜡烛 + 成交量双图 (X 轴联动), 复用 CandlestickItem; 首次自动缩放,
  用户手动缩放/平移后 pyqtgraph 自动停跟随 (还原用「适应」按钮)。
"""

import os
import threading
from datetime import datetime

import pyqtgraph as pg
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QComboBox, QCheckBox, QPushButton,
)
from PyQt5.QtCore import Qt, pyqtSignal, QTimer
from PyQt5.QtGui import QColor, QIcon

from config import (
    CHART_COLOR_CANDLE_UP, CHART_COLOR_CANDLE_DOWN,
    CHART_COLOR_VOLUME_UP, CHART_COLOR_VOLUME_DOWN,
    CHART_COLOR_BG,
    COLOR_BG_DARK, COLOR_BG_PANEL, COLOR_TEXT, COLOR_TEXT_DIM,
    COLOR_BORDER, COLOR_ACCENT, COLOR_GREEN, COLOR_RED,
)
from widgets.candlestick_item import CandlestickItem
from widgets.ui_util import disable_ime

try:
    from zoneinfo import ZoneInfo
    _ET_TZ = ZoneInfo("America/New_York")
except Exception:       # 无 tzdata → 退回本地时间显示
    _ET_TZ = None

_REFRESH_MS = 10_000    # 自动刷新间隔


class _TimeAxis(pg.AxisItem):
    """X 轴按 bar 序号显示 HH:MM (美东)。"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._labels: list[str] = []

    def set_labels(self, labels: list[str]):
        self._labels = labels

    def tickStrings(self, values, scale, spacing):
        out = []
        for v in values:
            idx = int(round(v))
            out.append(self._labels[idx] if 0 <= idx < len(self._labels) else "")
        return out


class OptionChartWindow(QMainWindow):
    """单个期权合约的今日 1 分钟图 (独立窗口)。"""

    _bars_loaded = pyqtSignal(list, str)   # (bars, error_msg — "" 表示成功)

    def __init__(self, engine, option, parent=None):
        super().__init__(parent)
        self._engine = engine
        self._option = option
        self._loading = False
        self._closed = False

        self.setWindowTitle(f"期权1分图 — {option.display_name} (今日)")
        # 独立窗口 (parent=None) 不吃主窗口的图标, 不显式设就是 pythonw 的默认图标 ——
        # 每次切换合约弹出来的"python 加载窗口"就是它。
        _icon = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.ico"
        )
        if os.path.exists(_icon):
            self.setWindowIcon(QIcon(_icon))
        self.setMinimumSize(700, 420)
        self.resize(950, 560)
        # 关闭即销毁 → destroyed 信号让主窗口把本窗从 _chart_windows 列表移除
        self.setAttribute(Qt.WA_DeleteOnClose, True)

        self._build_ui()
        self._apply_style()
        disable_ime(self)   # 见 ui_util: 别让搜狗挂上来
        self._bars_loaded.connect(self._on_bars)

        self._timer = QTimer(self)
        self._timer.setInterval(_REFRESH_MS)
        self._timer.timeout.connect(self._load)
        self._timer.start()

    def show_and_load(self):
        self.show()
        self._load()

    def set_option(self, option):
        """就地换合约 —— 不销毁重建窗口。

        原先双击另一个合约是 close() 旧窗 + new 一个新窗: 每次都要重建 pyqtgraph
        的两个 PlotWidget、重跑一遍 _build_ui/_apply_style, 还会在任务栏闪一个新窗口。
        就地换只留下真正必需的那部分开销 —— 拉历史数据。
        """
        self._option = option
        self._loading = False        # 旧合约那次请求的结果由 _on_bars 按 _option 丢弃
        self._first_load_done = False  # 新合约重新自适应坐标轴
        self.setWindowTitle(f"期权1分图 — {option.display_name} (今日)")
        self._name_label.setText(option.display_name)
        self._ohlc_label.setText("--")
        self._status_label.setText("加载中…")
        self._load()

    # ── UI ────────────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(2)

        bar = QHBoxLayout()
        bar.setSpacing(8)

        name = QLabel(self._option.display_name)
        name.setStyleSheet(
            f"color: {COLOR_ACCENT}; font-size: 13px; font-weight: bold; border: none;"
        )
        self._name_label = name   # set_option 换合约时要改它
        bar.addWidget(name)

        self._src_combo = QComboBox()
        self._src_combo.addItem("成交价", "TRADES")
        self._src_combo.addItem("中间价", "MIDPOINT")
        self._src_combo.setFixedWidth(84)
        self._src_combo.setToolTip(
            "成交价(TRADES): 实际成交 K 线, 无成交的分钟没有 bar;\n"
            "中间价(MIDPOINT): (bid+ask)/2, 稀疏合约更连续但无成交量。"
        )
        self._src_combo.currentIndexChanged.connect(lambda _i: self._load(force=True))
        bar.addWidget(self._src_combo)

        self._auto_cb = QCheckBox("自动刷新(10s)")
        self._auto_cb.setChecked(True)
        self._auto_cb.toggled.connect(
            lambda on: self._timer.start() if on else self._timer.stop()
        )
        bar.addWidget(self._auto_cb)

        refresh_btn = QPushButton("🔄 刷新")
        refresh_btn.setFixedHeight(24)
        refresh_btn.clicked.connect(lambda: self._load(force=True))
        bar.addWidget(refresh_btn)

        fit_btn = QPushButton("适应")
        fit_btn.setFixedHeight(24)
        fit_btn.setToolTip("恢复自动缩放/跟随最新")
        fit_btn.clicked.connect(self._auto_range)
        bar.addWidget(fit_btn)

        bar.addStretch()

        self._ohlc_label = QLabel("--")
        self._ohlc_label.setStyleSheet(
            f"color: {COLOR_TEXT}; font-size: 12px; border: none;"
        )
        bar.addWidget(self._ohlc_label)

        self._status_label = QLabel("")
        self._status_label.setStyleSheet(
            f"color: {COLOR_TEXT_DIM}; font-size: 11px; border: none;"
        )
        bar.addWidget(self._status_label)

        layout.addLayout(bar)

        # ── 图区: 价格 (蜡烛) + 成交量, X 轴联动 ──
        self._glw = pg.GraphicsLayoutWidget()
        self._glw.setBackground(CHART_COLOR_BG)

        self._time_axis = _TimeAxis(orientation="bottom")
        self._price_plot = self._glw.addPlot(row=0, col=0)
        self._price_plot.showGrid(x=True, y=True, alpha=0.15)
        self._price_plot.hideAxis("bottom")

        # solid=True: 涨跌都实心填充、主体不描边 — 全天 390 根 1min 高密度下
        # 空心轮廓+cosmetic 描边会把相邻柱糊成一片 (柱子黏连)
        self._candles = CandlestickItem(
            color_up=CHART_COLOR_CANDLE_UP, color_down=CHART_COLOR_CANDLE_DOWN,
            solid=True,
        )
        self._price_plot.addItem(self._candles)

        self._vol_plot = self._glw.addPlot(row=1, col=0, axisItems={"bottom": self._time_axis})
        self._vol_plot.setMaximumHeight(110)
        self._vol_plot.showGrid(x=False, y=True, alpha=0.15)
        self._vol_plot.setXLink(self._price_plot)
        self._vol_item: pg.BarGraphItem | None = None

        layout.addWidget(self._glw)

    def _apply_style(self):
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{ background-color: {COLOR_BG_DARK}; }}
            QComboBox, QPushButton, QCheckBox {{
                color: {COLOR_TEXT}; background-color: {COLOR_BG_PANEL};
                border: 1px solid {COLOR_BORDER}; padding: 2px 6px; font-size: 12px;
            }}
            QCheckBox {{ background: transparent; border: none; }}
        """)

    # ── 数据加载 ──────────────────────────────────────────────────────

    def _load(self, force: bool = False):
        if self._loading or self._closed:
            return
        if not getattr(self._engine, "is_connected", False):
            self._status_label.setText("未连接")
            return
        self._loading = True
        self._status_label.setText("加载中…")
        o = self._option
        what = self._src_combo.currentData()
        # 换合约后旧请求可能还在路上 (历史数据 timeout 给到 25 秒), 回来时窗口已经在
        # 显示新合约了 —— 用序号把迟到的结果丢掉, 否则会把上一个合约的 K 线画上去。
        self._load_seq = getattr(self, "_load_seq", 0) + 1
        seq = self._load_seq

        def worker():
            try:
                bars = self._engine.request_option_historical_data(
                    o.symbol, o.expiry, o.strike, o.right,
                    bar_size="1 min", duration="1 D",
                    what_to_show=what, timeout=25,
                )
                err = ""
            except Exception as e:
                bars, err = [], str(e)
            if seq != getattr(self, "_load_seq", seq):
                return  # 已经切到别的合约了, 这份数据作废
            try:
                self._bars_loaded.emit(bars, err)
            except RuntimeError:
                pass  # 窗口已关闭销毁 (WA_DeleteOnClose), 丢弃迟到的数据

        threading.Thread(target=worker, daemon=True).start()

    def _on_bars(self, bars: list, err: str):
        self._loading = False
        if self._closed:
            return
        if err:
            # 常见: 该合约今天还没有成交 (TRADES 无数据) → 提示切中间价
            hint = " (可切「中间价」)" if self._src_combo.currentData() == "TRADES" else ""
            self._status_label.setText(f"无数据/出错{hint}: {err[:60]}")
            return
        if not bars:
            self._status_label.setText("今日暂无 K 线")
            return

        data, labels, vols = [], [], []
        for i, b in enumerate(bars):
            try:
                ts = int(float(b["date"]))
                dt = (datetime.fromtimestamp(ts, _ET_TZ) if _ET_TZ
                      else datetime.fromtimestamp(ts))
                labels.append(dt.strftime("%H:%M"))
            except (ValueError, TypeError, OSError):
                labels.append("")
            data.append({
                "date_idx": i, "open": b["open"], "high": b["high"],
                "low": b["low"], "close": b["close"],
            })
            vols.append(max(int(b.get("volume", 0) or 0), 0))

        self._time_axis.set_labels(labels)
        self._candles.set_data(data)

        # 成交量柱 (涨绿跌红); 中间价模式 volume 全 0 → 空图
        if self._vol_item is not None:
            self._vol_plot.removeItem(self._vol_item)
        brushes = [
            QColor(CHART_COLOR_VOLUME_UP) if d["close"] >= d["open"]
            else QColor(CHART_COLOR_VOLUME_DOWN)
            for d in data
        ]
        self._vol_item = pg.BarGraphItem(
            x=list(range(len(vols))), height=vols, width=0.6, brushes=brushes,
            pen=pg.mkPen(None),
        )
        self._vol_plot.addItem(self._vol_item)

        last = data[-1]
        day_open = data[0]["open"]
        chg = last["close"] - day_open
        pct = (chg / day_open * 100) if day_open else 0.0
        color = COLOR_GREEN if chg >= 0 else COLOR_RED
        sign = "+" if chg >= 0 else ""
        self._ohlc_label.setText(
            f'最新 <b style="color:{color}">{last["close"]:.2f}</b> '
            f'<span style="color:{color}">{sign}{chg:.2f} ({sign}{pct:.1f}%)</span>  '
            f'高 {max(d["high"] for d in data):.2f} '
            f'低 {min(d["low"] for d in data):.2f}  共{len(data)}根'
        )
        self._status_label.setText(datetime.now().strftime("已更新 %H:%M:%S"))

        if not getattr(self, "_first_load_done", False):
            self._first_load_done = True
            self._auto_range()

    def _auto_range(self):
        self._price_plot.enableAutoRange(axis=pg.ViewBox.XYAxes, enable=True)
        self._vol_plot.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)

    # ── 生命周期 ──────────────────────────────────────────────────────

    def cleanup(self):
        """与 ChartWindow 同名接口 — 主窗口 closeEvent 对 `_chart_windows`
        里所有图表统一调用 (缺此方法曾致退出时 AttributeError, 后续
        cond_manager/engine 清理被跳过)。"""
        self._closed = True
        self._timer.stop()

    def closeEvent(self, event):
        self._closed = True
        self._timer.stop()
        super().closeEvent(event)
