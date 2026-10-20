# V5通知切换（2026-10-20）

本次生产接入不修改V5形态/CVD门槛或历史回测文件。

```dotenv
TELEGRAM_ENABLED=true
FIVE_MINUTE_TELEGRAM_ENABLED=false
HOURLY_ENABLED=true
HOURLY_TELEGRAM_ENABLED=true
HOURLY_STRATEGY=sweep_v5
```

沿用现有Telegram配置与Coinalyze数据来源，不在Git记录密钥。
默认5m开关为true，以保持未执行本次配置的旧环境兼容。
生产显式设为false：5m采集、信号入库、快照和结果追踪继续；5m Telegram客户端关闭，
其启动/异常提醒也不再投递。1H Telegram独立启用，且仅允许V5 kind出箱。

实时入口先检查全部输入的`causal`与`strategy_eligible`，再复用冻结的下一根确认规则。
最多169根小时输入，确保扫点前仍有168根上下文；未收盘、缺口、来源变化继续阻断。
首次启动重建仅记录历史。迟于理论决策10分钟的确认记录为stale_delivery，不补发。
旧1H pending队列标为superseded_strategy；已attempted/uncertain的消息不重试，以免重复。
V5冷却仅按V5 kind计算，不被旧1H信号误抑制。外部投递仍受单写者锁、fresh ticker和失效位检查保护。

消息明确标注V5、原扫点/确认小时、CVD状态（持续分化/转向兑现），评分是原扫点证据分，非胜率。
当前上线沿用结构失效位，不加入下单、自动交易、拟合止盈或历史通知重放。
`/health`提供five_minute_telegram_enabled与five_minute_recording_enabled；
`/hourly/health`提供策略版本、telegram_enabled、active_signal_kinds、评估与采集健康。

回退通知：关闭HOURLY_TELEGRAM_ENABLED；回退算法可设HOURLY_STRATEGY=sweep。
重新启用5m通知必须显式设FIVE_MINUTE_TELEGRAM_ENABLED=true。
回退不删除历史、信号、快照、结果或原始观测。
