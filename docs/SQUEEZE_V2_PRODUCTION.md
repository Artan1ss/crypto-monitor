# Squeeze V2 与 1H V5 生产通知

仅部署独立Squeeze V2运行时，沿用已提交的1H V5门槛，不包含本机反转研究或面板实验。

```dotenv
TELEGRAM_ENABLED=true
FIVE_MINUTE_TELEGRAM_ENABLED=false
HOURLY_ENABLED=true
HOURLY_TELEGRAM_ENABLED=true
HOURLY_STRATEGY=sweep_v5
SQUEEZE_ENABLED=true
SQUEEZE_TELEGRAM_ENABLED=true
```

不复制密钥到仓库，沿用现有Railway变量。仍保持私网，未开放公网。

共享现有Coinalyze客户端及每分钟符号预算，不另开并发限流器。原生15min取固定三现货与三合约；现货要求当前有效、绑定provider symbol的独立单位证明。15m原始记录用独立source_type，绝不进入旧1H输入选择器。
收盘至少180秒后计算。启动先用最多12H历史预热，首启历史仅入库，不发TG；只允许启用之后的新收盘事件。迟于理论判定10分钟不发。缺失的最近K线在期限内重试，不插值、不强行满足形态。

持久化保存最近15根冻结输入与增量事件状态，和唯一事件outbox在同一事务提交。滚动部署用独立Postgres会话锁，投递前再次核验锁所有权。同事件不覆盖证据，重启不重发。
网络调用前将pending比较交换为attempted；超时或拒绝记录uncertain、不自动重试，避免重复通知。跨数据库/TG不能保证exactly-once；attempted/uncertain需要人工核对。
WATCH为积累观察；RELEASE是突破及OI回落迹象，不是提前预警，只有父级WATCH已通知才可通知。释放标记沿用原观察分，非释放成功率。无买卖指令或收益回测。

## 验收

- Alembic升级到0006_squeeze_monitor，保留已有原始/小时数据。
- /hourly/health确认hourly_strategy=sweep_v5、enabled/telegram_enabled/healthy为true，输入没有阻断。
- /squeeze/health确认V2、enabled/telegram_enabled/healthy及last_decision；delivery_counts可查看历史抑制与投递状态。
- /health确认five_minute_telegram_enabled=false。archive/health的coinalyze需healthy及normalization_ready为true。
- /squeeze/signals列出真实产生的记录；/squeeze/signals/{UUID}/export可导出完整冻结快照及原始fingerprint输入。
- 云端控制台运行 `python -m btc_monitor.squeeze.verify` 做只读核验。
- 人工验收时仅一次运行 `python -m btc_monitor.squeeze.verify --send-test`。所有健康检查通过才发明确标注的通道测试，不插入伪造策略事件。

回退仅设SQUEEZE_TELEGRAM_ENABLED=false；需要停采集再设SQUEEZE_ENABLED=false，不删除证据与事件状态。1H通知独立控制；5m继续不投递。
