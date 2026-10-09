#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
notify.py —— 从 renew.log 里解析结果，发一条带「下次可续期时间」的 Telegram 通知。

为什么不改上游 app.py：
  app.py 的 send_tg_message() 明明有 time_left 参数，但压根没用进消息里，
  页面给的续期窗口信息就被丢掉了。与其改上游（以后同步会冲突），
  不如把通知逻辑整个搬到我们自己仓库 —— 格式随便调，还能带上运行链接。

用法（在 workflow 里）：
    TG_BOT_TOKEN / TG_CHAT_ID / RUN_URL / RUN_NUMBER / KATABUMP_EMAIL 走环境变量
    DRY_RUN=1  → 只打印，不发
"""

import os
import re
import sys
import json
import datetime
import urllib.request
import urllib.parse

LOG_FILE = os.environ.get("RENEW_LOG", "renew.log")
TG_BOT_TOKEN = (os.environ.get("TG_BOT_TOKEN") or "").strip()
TG_CHAT_ID = (os.environ.get("TG_CHAT_ID") or "").strip()
RUN_URL = (os.environ.get("RUN_URL") or "").strip()
RUN_NUMBER = (os.environ.get("RUN_NUMBER") or "").strip()
EMAIL = (os.environ.get("KATABUMP_EMAIL") or "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def utcnow():
    """不带时区信息的 UTC now（避开 utcnow() 的弃用警告）"""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def mask_email(email):
    """15****18@qq.com —— 保留用户名前 2 后 2"""
    if not email:
        return "（未配置）"
    if "@" not in email:
        return email[:2] + "****"
    name, domain = email.split("@", 1)
    if len(name) > 4:
        return f"{name[:2]}****{name[-2:]}@{domain}"
    return f"{name}@{domain}"


def grab(pattern, text, flags=0, group=1):
    m = re.search(pattern, text, flags)
    return m.group(group).strip() if m else ""


def parse_next_renew(alert, today):
    """从页面提示里抠出「下次可续期日期」。

    能识别的形态：
      ... You will be able to as of 13 October (in 4 day(s)).   ← 主用这个
      ... as of October 13, 2026
      ... in 4 day(s)
    返回 (date 或 None, 说明字符串)
    """
    if not alert:
        return None, ""

    # 形态 1：as of 13 October   /   as of October 13
    m = re.search(r"as of\s+(\d{1,2})\s+([A-Za-z]+)", alert)
    if not m:
        m2 = re.search(r"as of\s+([A-Za-z]+)\s+(\d{1,2})", alert)
        if m2:
            mon_name, day = m2.group(1), int(m2.group(2))
            mon = MONTHS.get(mon_name.lower())
            if mon:
                return _safe_date(today.year, mon, day, today), ""

    if m:
        day = int(m.group(1))
        mon = MONTHS.get(m.group(2).lower())
        if mon:
            return _safe_date(today.year, mon, day, today), ""

    # 形态 2：退而求其次，用 "(in N day(s))" 推算
    m = re.search(r"in\s+(\d+)\s*day", alert)
    if m:
        return today + datetime.timedelta(days=int(m.group(1))), "（按页面提示的天数推算）"

    return None, ""


def _safe_date(year, month, day, today):
    """拼日期；如果算出来已经过去了，说明跨年，+1 年"""
    try:
        d = datetime.date(year, month, day)
    except ValueError:
        return None
    if d < today:
        try:
            d = datetime.date(year + 1, month, day)
        except ValueError:
            return None
    return d


def build_message(log_text, today):
    exit_ip = grab(r"当前出口IP:\s*([0-9a-fA-F\.:]+)", log_text)
    proxy = grab(r"挂载代理:\s*(\S+)", log_text)
    direct = "未使用代理" in log_text
    alert = grab(r"页面提示:\s*(.+)", log_text)

    if "❌ 登录失败" in log_text:
        icon, status = "❌", "登录失败"
    elif "❌ Turnstile" in log_text:
        icon, status = "⚠️", "Turnstile 没过去"
    elif re.search(r"renewed|success|extended", alert, re.I):
        icon, status = "🎉", "续期成功"
    elif re.search(r"can'?t renew|unable", alert, re.I):
        icon, status = "⏳", "未到续期窗口"
    elif alert:
        icon, status = "ℹ️", "已执行，结果看页面提示"
    else:
        icon, status = "❓", "没拿到明确结果"

    next_date, note = parse_next_renew(alert, today)

    lines = ["🇫🇷 katabump 续期通知", "", f"{icon} {status}"]
    lines.append(f"👤 账户: {mask_email(EMAIL)}")

    if next_date:
        # 只报绝对日期 + 星期。相对天数（"N 天后"）不报：
        # katabump 页面按它自己的时区算（实测 "(in 4 day(s))"），北京看会差一天，
        # 报出来只会让人怀疑是不是算错了。
        lines.append(f"📅 下次可续期: {next_date.isoformat()}（{WEEKDAYS[next_date.weekday()]}）{note}")

    bj = utcnow() + datetime.timedelta(hours=8)
    lines.append(f"🕒 本次执行: {bj.strftime('%Y-%m-%d %H:%M')}（北京时间）")

    if exit_ip:
        via = "已挂代理" if proxy else ("直连 ⚠️" if direct else "")
        lines.append(f"🌐 出口 IP: {exit_ip}" + (f"（{via}）" if via else ""))

    if RUN_NUMBER:
        lines.append(f"🔁 第 {RUN_NUMBER} 次运行")
    if RUN_URL:
        lines.append(f"🔗 {RUN_URL}")

    return "\n".join(lines)


def send(text):
    if DRY_RUN:
        print("[DRY_RUN] 不会真的发送，消息内容：")
        print("-" * 46)
        print(text)
        print("-" * 46)
        return True
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("ℹ️ 没配 TG_BOT_TOKEN / TG_CHAT_ID，跳过推送")
        return False
    payload = urllib.parse.urlencode({
        "chat_id": TG_CHAT_ID,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
        data=payload,
        headers={"User-Agent": "katabump-renew-notify"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        if d.get("ok"):
            print("📩 Telegram 通知发送成功！")
            return True
        print(f"⚠️ Telegram 通知发送失败: {d}")
    except Exception as e:
        print(f"⚠️ Telegram 通知发送异常: {type(e).__name__}: {e}")
    return False


def main():
    if os.path.isfile(LOG_FILE):
        log_text = open(LOG_FILE, encoding="utf-8", errors="replace").read()
    else:
        log_text = ""
        print(f"⚠️ 找不到 {LOG_FILE}")

    today = utcnow().date()
    text = build_message(log_text, today)
    print(text)
    print("-" * 46)
    send(text)


if __name__ == "__main__":
    sys.exit(0 if main() is None else 0)
