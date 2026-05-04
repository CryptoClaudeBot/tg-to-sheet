#!/usr/bin/env python3
"""
Telegram -> Google Sheet 同步脚本
每小时运行一次，把"@ 了 Bot"的群消息用 Gemini 智能解析后写入 Google Sheet。

触发规则：
    1) 消息文本里包含 @<botusername>
    2) 或者消息是「回复 Bot 的某条消息」
    3) 或者消息里用了 text_mention 提及 Bot

依赖（见 requirements.txt）:
    requests, gspread, google-auth, google-generativeai

需要的环境变量:
    TELEGRAM_BOT_TOKEN          - BotFather 给你的 Bot Token
    GOOGLE_SHEET_ID             - 目标 Google Sheet 的 ID（URL 里 /d/ 后面那段）
    GOOGLE_SERVICE_ACCOUNT_JSON - Service Account 的 JSON 密钥（整段内容）
    GEMINI_API_KEY              - Google AI Studio 拿的 Gemini API Key
    TELEGRAM_GROUP_CHAT_ID      - (可选) 限定只处理这个群的消息
"""

import os
import sys
import json
from datetime import datetime, timezone

import requests
import gspread
from google.oauth2.service_account import Credentials
import google.generativeai as genai


# ============== 配置 ==============
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GROUP_CHAT_ID = os.environ.get("TELEGRAM_GROUP_CHAT_ID")

DATA_TAB = "数据"
STATE_TAB = "_state"

# ============== 字段定义 ==============
# (列名, LLM 用来判断该字段该填什么的描述)
# 想增删/改字段，直接编辑这个列表即可。
# 注意：如果你已经有一个 `数据` tab 且表头跟这里不一致，需要先把那个 tab 删掉，
# 让脚本下次跑时自动按新字段重新建表头。
FIELD_DEFINITIONS = [
    ("项目名", "加密货币项目的名字，英文为主。如果没有提到留空。"),
    ("估值", "项目估值，统一换成纯数字字符串。'10k' → '10000'，'1.5m' → '1500000'，'2亿' → '200000000'。如果没有提到留空。"),
    ("类别", "项目的赛道或类别，比如 DeFi / 隐私 / L2 / AI / Meme / RWA / SocialFi / Infra 等。如果没有提到留空。"),
    ("轮次", "融资轮次。比如 Pre-seed / Seed / A轮 / B轮 / Strategic 等。如果没有提到留空。"),
    ("官网", "项目官方网站的 URL（不是 deck 链接）。如果消息里没有官网 URL 留空。"),
    ("Deck", "项目 deck 的链接或附件文件名。如果消息附带 PDF/PPT/PPTX 文件，写文件名（如 'deck.pdf'）；如果是 URL（链接里出现 docsend / drive / dropbox / notion / pitch.com 这类关键字），写完整 URL；都没有就留空。"),
    ("联系人", "项目方对接人的名字（注意：不是消息发送者本人，是消息里提到的对方负责人）。如果没有提到留空。"),
    ("备注", "其他没被上面字段覆盖的关键信息，简短归纳一两句话。如果没有特别信息留空。"),
]

FIELD_NAMES = [name for name, _ in FIELD_DEFINITIONS]


# ============== Gemini 配置 ==============
genai.configure(api_key=GEMINI_API_KEY)

SYSTEM_PROMPT = """你是一个加密货币项目信息解析助手。从用户给你的 Telegram 群消息里抽取结构化字段。

规则：
- 严格按照定义的 JSON Schema 输出，所有字段都必须出现，没提到的字段输出空字符串 ""
- 字段值必须是字符串类型（即使是数字也用字符串表示）
- 不要编造消息里没有的信息——拿不准就留空
- 估值统一换成纯数字字符串（"10k" → "10000"，"1.5m" → "1500000"，"$2M" → "2000000"）
- 不要把消息发送者自己当成"联系人"，"联系人"指消息里提到的对方负责人

字段说明：
""" + "\n".join(f"- {name}: {desc}" for name, desc in FIELD_DEFINITIONS)

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {name: {"type": "string"} for name, _ in FIELD_DEFINITIONS},
    "required": FIELD_NAMES,
}

_GEMINI_MODEL = genai.GenerativeModel(
    "gemini-2.0-flash",
    system_instruction=SYSTEM_PROMPT,
)


def parse_with_llm(text: str) -> dict:
    """调用 Gemini 解析消息，返回 {字段名: 值} 字典；失败时返回全空。"""
    if not text.strip():
        return {f: "" for f in FIELD_NAMES}
    try:
        response = _GEMINI_MODEL.generate_content(
            text,
            generation_config={
                "response_mime_type": "application/json",
                "response_schema": RESPONSE_SCHEMA,
                "temperature": 0,
            },
        )
        result = json.loads(response.text)
        # 防御性补齐 + 类型转字符串
        return {f: str(result.get(f, "") or "") for f in FIELD_NAMES}
    except Exception as exc:
        print(f"[warn] LLM parse failed: {exc}")
        return {f: "" for f in FIELD_NAMES}


# ============== Bot mention 判断 ==============
def is_bot_mentioned(msg: dict, bot_username: str) -> bool:
    bot_username_lower = bot_username.lower()
    text = msg.get("text") or msg.get("caption") or ""
    if f"@{bot_username_lower}" in text.lower():
        return True
    reply = msg.get("reply_to_message") or {}
    reply_from = reply.get("from") or {}
    if reply_from.get("is_bot") and (reply_from.get("username") or "").lower() == bot_username_lower:
        return True
    entities = (msg.get("entities") or []) + (msg.get("caption_entities") or [])
    for ent in entities:
        if ent.get("type") == "text_mention":
            user = ent.get("user") or {}
            if (user.get("username") or "").lower() == bot_username_lower:
                return True
    return False


def build_llm_input(msg: dict) -> str:
    """合成给 LLM 看的内容：消息文本 + 附件文件名（如果有）。"""
    parts = []
    text = msg.get("text") or msg.get("caption") or ""
    if text:
        parts.append(text)
    doc = msg.get("document")
    if doc:
        parts.append(f"[附件: {doc.get('file_name', '未命名')}]")
    return "\n".join(parts)


# ============== Telegram ==============
def get_bot_username() -> str:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getMe"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram getMe error: {data}")
    username = data["result"].get("username")
    if not username:
        raise RuntimeError("Bot has no username")
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
    creds = Credentials.from_service_account_info(
        creds_info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gspread.authorize(creds).open_by_key(SHEET_ID)


def ensure_tabs(sheet):
    titles = [ws.title for ws in sheet.worksheets()]
    if STATE_TAB not in titles:
        ws = sheet.add_worksheet(title=STATE_TAB, rows=2, cols=2)
        ws.update("A1:B1", [["key", "value"]])
        ws.update("A2:B2", [["last_update_id", "0"]])
    if DATA_TAB not in titles:
        ws = sheet.add_worksheet(title=DATA_TAB, rows=1000, cols=4 + len(FIELD_NAMES) + 5)
        header = ["时间", "发送人", "原始消息", "message_id"] + FIELD_NAMES
        ws.update("A1", [header])


def get_last_update_id(sheet) -> int:
    val = sheet.worksheet(STATE_TAB).acell("B2").value
    try:
        return int(val) if val else 0
    except ValueError:
        return 0


def set_last_update_id(sheet, value: int):
    sheet.worksheet(STATE_TAB).update("B2", [[str(value)]])


def append_rows(sheet, rows):
    sheet.worksheet(DATA_TAB).append_rows(rows, value_input_option="USER_ENTERED")


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

        chat_id = msg.get("chat", {}).get("id")
        if GROUP_CHAT_ID and str(chat_id) != str(GROUP_CHAT_ID):
            continue

        if not is_bot_mentioned(msg, bot_username):
            continue

        llm_input = build_llm_input(msg)
        if not llm_input.strip():
            continue  # 纯贴纸/纯图片消息没文本可解析，跳过

        print(f"[info] parsing message_id={msg.get('message_id')}: {llm_input[:80]!r}")
        parsed = parse_with_llm(llm_input)

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

        original = msg.get("text") or msg.get("caption") or ""
        doc = msg.get("document")
        if doc:
            original = (original + f"\n[附件: {doc.get('file_name', '未命名')}]").strip()

        row = [ts, sender, original, message_id] + [parsed.get(f, "") for f in FIELD_NAMES]
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
