"""Cloud-local health verification and explicit, labelled Telegram channel test."""
import argparse
import asyncio
import json
import os
from datetime import UTC,datetime,timedelta

import httpx
from btc_monitor.config import Settings
from btc_monitor.telegram import TelegramClient
from btc_monitor.squeeze.service import quarter


async def verify(send_test=False):
    settings=Settings()
    auth=httpx.BasicAuth("monitor",settings.dashboard_password.get_secret_value()) if settings.dashboard_password else None
    base="http://127.0.0.1:"+os.getenv("PORT","8000")
    async with httpx.AsyncClient(base_url=base,auth=auth,timeout=20) as client:
        values={}
        for path in ("/health","/hourly/health","/archive/health","/squeeze/health"):
            response=await client.get(path)
            if response.status_code not in (200,503):
                raise RuntimeError("health_endpoint_unavailable")
            values[path]=response.json()
    h=values["/hourly/health"];s=values["/squeeze/health"];p=values["/archive/health"].get("coinalyze",{})
    now=datetime.now(UTC)
    latest_squeeze=quarter(now-timedelta(seconds=settings.squeeze_decision_delay_seconds)).isoformat()
    sq_decision=s.get("last_decision") or {}
    checks={"hourly_v5":h.get("parameters",{}).get("hourly_strategy")=="sweep_v5",
        "hourly_running":h.get("healthy") is True,"hourly_telegram":h.get("telegram_enabled") is True,
        "hourly_inputs_ready":bool(h.get("last_evaluation")) and (h.get("last_evaluation") or {}).get("blocked") is None,
        "squeeze_v2":s.get("version","").startswith("squeeze-15m-morphology-v2"),
        "squeeze_running":s.get("healthy") is True,"squeeze_telegram":s.get("telegram_enabled") is True,
        "squeeze_inputs_current":sq_decision.get("latest_end")==latest_squeeze and sq_decision.get("blocked") is None,
        "provider_ready":p.get("healthy") is True and p.get("normalization_ready") is True,
        "five_minute_muted":values["/health"].get("five_minute_telegram_enabled") is False}
    report={"checked_at":datetime.now(UTC).isoformat(),"checks":checks,
        "squeeze_delivery_counts":s.get("delivery_counts"),
        "hourly_last_evaluation":h.get("last_evaluation"),"squeeze_last_decision":s.get("last_decision"),
        "all_ready":all(checks.values()),"test_message_id":None}
    if send_test and report["all_ready"]:
        telegram=TelegramClient(True,settings.telegram_bot_token,settings.telegram_chat_id)
        try:
            report["test_message_id"]=await telegram.send(
                "<b>🧪 BTC Monitor 生产通道自检 · 非行情信号</b>\n"
                "1H V5：已加载，计算与TG开关正常\n"
                "15m 挤压V2：已运行，积累观察／释放迹象分级通知已启用\n"
                "Coinalyze六源指标与单位验证：正常\n"
                "5m通知：关闭，仅记录\n"
                "本条仅验收发送通道，不是实际策略触发；不补发历史、不下单。")
        finally:
            await telegram.close()
    print(json.dumps(report,ensure_ascii=False))
    return report["all_ready"] and (not send_test or report["test_message_id"] is not None)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send-test",action="store_true")
    args=parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args.send_test)) else 2)
