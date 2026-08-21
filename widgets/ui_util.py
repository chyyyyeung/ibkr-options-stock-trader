"""UI 小工具。"""

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QAbstractSpinBox, QLineEdit


def disable_ime(root) -> int:
    """关掉 root 及其所有子控件上的输入法挂载, 返回处理的控件数。

    **为什么**: 本机装了搜狗拼音 (SogouTSF.ime / SogouPY.ime 会被加载进本进程)。
    焦点一落进文本框, 搜狗就通过 TSF 挂上来, 弹出一个 `SoPY_Status` 状态栏窗口
    —— 那窗口是在**本进程**里创建的, 没标题没图标, 看起来就像"程序自己弹了一个
    python 空窗", 同时挂载 + 云组件初始化会卡 1-3 秒。双击监控里的合约切换时,
    `PriceLadder.set_option` 里 `search_input.setText("")` 一碰输入框就会触发。
    (实测: 主线程并没有卡死 —— IsHungAppWindow 全程未触发, 卡顿全在输入法侧。)

    **为什么可以一刀切**: 本程序所有输入框只输 ASCII —— 合约代码 (TSLA260610P385000)、
    标的代码 (IBM)、数量/价格数字。没有任何一个需要中文, 所以不必挑挑拣拣。
    将来若加了需要中文的输入框 (备注之类), 在它上面单独
    `setAttribute(Qt.WA_InputMethodEnabled, True)` 反悔即可。

    findChildren(QLineEdit) 会连 QSpinBox / 可编辑 QComboBox **内部那个**
    QLineEdit 一起找出来, 所以不用分别处理各种控件类型。
    """
    targets = []
    if isinstance(root, (QLineEdit, QAbstractSpinBox)):
        targets.append(root)
    targets += root.findChildren(QLineEdit)
    targets += root.findChildren(QAbstractSpinBox)

    for w in targets:
        # 硬开关: Qt 据此判定该控件是否接受输入法事件, 关掉 TSF 就不会来挂
        w.setAttribute(Qt.WA_InputMethodEnabled, False)
        # 再给一层提示: 部分输入法看 hints 自动切英文 (WA 被 Qt 内部重置时兜底)
        if isinstance(w, QAbstractSpinBox):
            w.setInputMethodHints(Qt.ImhDigitsOnly | Qt.ImhNoPredictiveText)
        else:
            w.setInputMethodHints(Qt.ImhLatinOnly | Qt.ImhNoPredictiveText)
    return len(targets)
