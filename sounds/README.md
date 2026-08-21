# 提示音 (sounds/)

真正成交时 `sound_alerts.play_fill()` 会按以下优先级播放:

1. `BUY.wav` / `BUY.mp3` —— 买入成交
2. `SELL.wav` / `SELL.mp3` —— 卖出成交
3. 找不到买/卖专用时回退 `FILL.wav` / `FILL.mp3`
4. 都没有 → winsound 蜂鸣 (买升调 / 卖降调)

自选监控到价警报 `sound_alerts.play_alert()`:

1. `ALERT.wav` / `ALERT.mp3` —— 到价警报 (当前为 misaka v3 生成的「価格到達よ」)
2. 没有 → winsound 三连高音蜂鸣

把自制提示音放到本目录、按上面文件名命名即可,无需改代码。
wav 优先于 mp3。建议短促 (<2s)。
