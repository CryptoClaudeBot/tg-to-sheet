#!/usr/bin/env python3
"""
Telegram -> Google Sheet 同步脚本
每小时运行一次，把"@ 了 Bot"的群消息解析后写入 Google Sheet。

触发规则：
    1) 消息文本里包含 @<botusername>
    2) 或者消息是「回复 Bot 的某条消息」
    3) 或者消息里用了 text_mention（手动点选 Bot 名字提及，无 username 时）
满足以上任一条件的消息都会被记录。

依赖（见 requirements.txt）:
    requests, gspread, google-auth

需要的环境变量:
    TELEGRAM_BOT_TOKEN          - BotFather 给你的 Bot Token
    GOOGLE_SHEET_ID             - 目标 Google Sheet 的 ID（URL 里 /d/ 后面那段）
    GOOGLE_SERVICE_ACCOUNT_JSON - Service Account 的 JSON 密钥（整段内容）
    TELEGRAM_GROUP_CHAT_ID      - (可选) 限定只处理这个群的消息，群 chat_id 通常是负数
"""

import os
import re
import sys
import json
from datetime import datetime, timezone

import requests
import gspread
from google.oauth2.service_account import Credentials


# ============== 配置 ==============
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
GROUP_CHAT_ID = os.environ.get("TELEGRAM_GROUP_CHAT_ID")  # 可选

DATA_TAB = "数据"        # 写入数据的工作表名
STATE_TAB = "_state"    # 保存 last_update_id 的工作表名

# 你想从消息里额外抽取的字段（顺序就是表格里的列顺序）
# 这些字段是「可选的」——只要 @ 了 Bot 就会被记录，没填这些字段对应列留空
EXPECTED_FIELDS = ["姓名", "日期", "金额"]


# ============== 解析 ==============
# 支持 全角/半角 冒号；key 允许中文/英文/数字/下划线
FIELD_PATTERN = re.compile(r"^(?P<key>[\u4e00-\u9fa5A-Za-z0-9_]+)\s*[：:]\s*(?P<value>.+)$")


def parse_fields(text: str) -> dict:
    """从消息文本里按 `字段：值` 格式抽取字段，返回字典（可能为空）。"""
    fields = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("@"):
            continue
        m = FIELD_PATTERN.match(line)
        if m:
            fields[m.group("key").strip()] = m.group("value").strip()
    return fields


def is_bot_mentioned(msg: dict, bot_username: str) -> bool:
    """判断消息是否 @ 了 Bot（含 reply 到 Bot 与 text_mention 两种特殊情况）。"""
    bot_username_lower = bot_username.lower()

    # 1) 文本里直接 @<username>
    text = msg.get("text") or msg.get("caption") or ""
    if f"@{bot_username_lower}" in text.lower():
        return True

    # 2) 回复 Bot 的某条消息
    reply = msg.get("reply_to_message") or {}
    reply_from = reply.get("from") or {}
    if reply_from.get("is_bot") and (reply_from.get("username") or "").lower() == bot_username_lower:
        return True

    # 3) text_mention 实体（用户手动从联系人里点选 Bot 提及，比较少见但完整起见兜住）
    entities = (msg.get("entities") or []) + (msg.get("caption_entities") or [])
    for ent in entities:
        if ent.get("type") == "text_mention":
            user = ent.get("user") or {}
            if (user.get("username") or "").lower() == bot_username_lower:
                return True

    return False


# ============== Telegram ==============
def get_bot_username() -> str:
    """通过 getMe 拿到 Bot 自己的 username，避免再加一个环境变量。"""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getMe"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram getMe error: {data}")
    username = data["result"].get("username")
    if not username:
        raise RuntimeError("Bot has no username, cannot detect mentions")
    return username


def get_telegram_updates(offset=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {"timeout": 0, "allowed_updates": '["message"]'}
    if offset is not None:
        params["offset"] = offset
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return data.get("result", [])


# ============== Google Sheet ==============
def get_sheet():
    creds_info = json.loads(SERVICE_ACCOUNT_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
    ]
    creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID)


def ensure_tabs(sheet):
    """确保 _state 和 数据 两个 tab 存在并初始化表头。"""
    titles = [ws.title for ws in sheet.worksheets()]
    if STATE_TAB not in titles:
        ws = sheet.add_worksheet(title=STATE_TAB, rows=2, cols=2)
        ws.update("A1:B1", [["key", "value"]])
        ws.update("A2:B2", [["last_update_id", "0"]])
    if DATA_TAB not in titles:
        ws = sheet.add_worksheet(title=DATA_TAB, rows=1000, cols=20)
        header = ["时间", "发送人", "原始消息", "message_id"] + EXPECTED_FIELDS
        ws.update("A1", [header])


def get_last_update_id(sheet) -> int:
    ws = sheet.worksheet(STATE_TAB)
    val = ws.acell("B2").value
    try:
        return int(val) if val else 0
    except ValueError:
        return 0


def set_last_update_id(sheet, value: int):
    ws = sheet.worksheet(STATE_TAB)
    ws.update("B2", [[str(value)]])


def append_rows(sheet, rows):
    ws = sheet.worksheet(DATA_TAB)
    ws.append_rows(rows, value_input_option="USER_ENTERED")


# ============== 主流程 ==============
def main():
    sheet = get_sheet()
    ensure_tabs(sheet)

    bot_username = get_bot_username()
    print(f"[info] bot username = @{bot_username}")

    last_id = get_last_update_id(sheet)
    offset = last_id + 1 if last_id else None

    updates = get_telegram_updates(offset=offset)
    print(f"[info] fetched {len(updates)} updates (offset={offset})")

    rows = []
    new_last_id = last_id

    for upd in updates:
        new_last_id = max(new_last_id, upd["update_id"])
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            continue

        text = msg.get("text") or msg.get("caption") or ""
        chat_id = msg.get("chat", {}).get("id")

        # 限定群（可选）
        if GROUP_CHAT_ID and str(chat_id) != str(GROUP_CHAT_ID):
            continue

        # 触发条件：消息要 @ 了 Bot
        if not is_bot_mentioned(msg, bot_username):
            continue

        parsed = parse_fields(text)  # 即使没有结构化字段也会记录（rows 里这几列留空）

        ts = (
            datetime.fromtimestamp(msg["date"], tz=timezone.utc)
            .astimezone()
            .strftime("%Y-%m-%d %H:%M:%S")
        )
        sender_obj = msg.get("from", {}) or {}
        sender = (
            sender_obj.get("username")
            or " ".join(filter(None, [sender_obj.get("first_name"), sender_obj.get("last_name")]))
            or str(sender_obj.get("id", ""))
        )
        message_id = msg.get("message_id", "")

        row = [ts, sender, text, message_id] + [parsed.get(f, "") for f in EXPECTED_FIELDS]
        rows.append(row)

    if rows:
        append_rows(sheet, rows)
        print(f"[info] appended {len(rows)} rows")
    else:
        print("[info] no @bot messages this run")

    if new_last_id != last_id:
        set_last_update_id(sheet, new_last_id)
        print(f"[info] updated last_update_id -> {new_last_id}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        sys.exit(1)
