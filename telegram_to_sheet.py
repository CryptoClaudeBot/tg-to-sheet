#!/usr/bin/env python3
"""
Telegram -> Google Sheet 同步脚本
每小时运行一次，把"@ 了 Bot"的群消息用 Anthropic Claude 智能解析后写入 Google Sheet。

触发规则：
    1) 消息文本里包含 @<botusername>
    2) 或者消息是「回复 Bot 的某条消息」
    3) 或者消息里用了 text_mention 提及 Bot

依赖（见 requirements.txt）:
    requests, gspread, google-auth, anthropic

需要的环境变量:
    TELEGRAM_BOT_TOKEN          - BotFather 给你的 Bot Token
    GOOGLE_SHEET_ID             - 目标 Google Sheet 的 ID（URL 里 /d/ 后面那段）
    GOOGLE_SERVICE_ACCOUNT_JSON - Service Account 的 JSON 密钥（整段内容）
    ANTHROPIC_API_KEY           - 在 console.anthropic.com 申请，sk-ant-... 开头
    TELEGRAM_GROUP_CHAT_ID      - (可选) 限定只处理这个群的消息
"""

import os
import sys
import json
from datetime import datetime, timezone

import requests
import gspread
from google.oauth2.service_account import Credentials
from anthropic import Anthropic


# ============== 配置 ==============
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GROUP_CHAT_ID = os.environ.get("TELEGRAM_GROUP_CHAT_ID")

DATA_TAB = "Data"
STATE_TAB = "_state"

# Claude 模型：Haiku 4.5 又快又便宜，足以做字段抽取
# 想用更强的可以换成 "claude-sonnet-4-6"，更便宜可以试已有的更小模型
CLAUDE_MODEL = "claude-haiku-4-5-20251001"


# ============== Field definitions ==============
# (internal_key, sheet_column_header, description for the LLM)
# Edit this list to add/remove fields. Both keys and headers should be ASCII.
# Note: if you already have a `Data` tab whose header doesn't match, delete that
# tab first so the script can recreate it with the new headers next run.
FIELD_DEFINITIONS = [
    ("project_name", "Project", "Name of the crypto project. Leave empty if not mentioned."),
    ("valuation", "Valuation", "Project valuation as a pure number string. '10k' -> '10000', '1.5m' -> '1500000', '$2M' -> '2000000'. Leave empty if not mentioned."),
    ("category", "Category", "Project's sector / category, e.g. DeFi, Privacy, L2, AI, Meme, RWA, SocialFi, Infra. Leave empty if not mentioned."),
    ("round", "Round", "Funding round, e.g. Pre-seed / Seed / Series A / Series B / Strategic. Leave empty if not mentioned."),
    ("website", "Website", "URL of the official project website (NOT a deck link). Leave empty if no website URL is in the message."),
    ("deck", "Deck", "The project's pitch deck — either an attached file name (e.g. 'deck.pdf') or a URL (typically containing docsend / drive / dropbox / notion / pitch.com). Leave empty if neither."),
    ("contact", "Contact", "Name of the contact person on the project side (NOT the message sender). Leave empty if not mentioned."),
    ("notes", "Notes", "Any other key information not covered by the fields above, summarized in one short sentence. Leave empty if nothing notable."),
]

FIELD_KEYS = [k for k, _, _ in FIELD_DEFINITIONS]                # internal keys
FIELD_DISPLAY_NAMES = [d for _, d, _ in FIELD_DEFINITIONS]       # sheet headers


# ============== Anthropic 配置 ==============
_anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """You extract structured fields about crypto projects from Telegram group messages.
The message may be in any language (English, Chinese, etc.) — interpret it correctly regardless.

Rules:
- You MUST call the extract_fields tool
- All fields must appear in the output. Use empty string "" for any field not mentioned
- All field values must be strings (wrap numbers in quotes)
- Do not invent information that isn't in the message — when in doubt, leave it empty
- For valuation, output a pure number string: "10k" -> "10000", "1.5m" -> "1500000", "$2M" -> "2000000"
- The "contact" field is the person on the project side, NOT the message sender

Field meanings (key -> description):
""" + "\n".join(f"- {k}: {desc}" for k, _, desc in FIELD_DEFINITIONS)

# tool_use forces structured JSON output — more reliable than free-form JSON in text
EXTRACT_TOOL = {
    "name": "extract_fields",
    "description": "Extract structured fields from a crypto-project-related Telegram message. All fields must appear; use empty string for missing ones.",
    "input_schema": {
        "type": "object",
        "properties": {key: {"type": "string", "description": desc} for key, _, desc in FIELD_DEFINITIONS},
        "required": FIELD_KEYS,
    },
}


def parse_with_llm(text: str) -> dict:
    """调用 Claude 解析消息，返回 {英文 key: 值} 字典；失败时返回全空。"""
    if not text.strip():
        return {k: "" for k in FIELD_KEYS}
    try:
        response = _anthropic_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=[EXTRACT_TOOL],
            tool_choice={"type": "tool", "name": "extract_fields"},
            messages=[{"role": "user", "content": text}],
        )
        for block in response.content:
            if block.type == "tool_use" and block.name == "extract_fields":
                result = block.input
                # 防御性补齐 + 类型转字符串
                return {k: str(result.get(k, "") or "") for k in FIELD_KEYS}
        print(f"[warn] no tool_use in response: {response}")
        return {k: "" for k in FIELD_KEYS}
    except Exception as exc:
        print(f"[warn] LLM parse failed: {exc}")
        return {k: "" for k in FIELD_KEYS}


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
    """Build the text we feed to the LLM: message text + attachment filename if any."""
    parts = []
    text = msg.get("text") or msg.get("caption") or ""
    if text:
        parts.append(text)
    doc = msg.get("document")
    if doc:
        parts.append(f"[Attachment: {doc.get('file_name', 'unnamed')}]")
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


def send_telegram_reply(chat_id, reply_to_message_id: int, text: str):
    """给指定消息发一条 reply。失败不抛异常，只打 warn。"""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text,
                "reply_to_message_id": reply_to_message_id,
                "allow_sending_without_reply": True,  # 原消息删掉了也照样发
            },
            timeout=10,
        )
        resp.raise_for_status()
        if not resp.json().get("ok"):
            print(f"[warn] reply not ok: {resp.text}")
    except Exception as exc:
        print(f"[warn] send reply failed (msg_id={reply_to_message_id}): {exc}")


def build_confirmation_text(parsed: dict) -> str:
    """根据解析结果生成回复文本。带上几个关键字段，发送者一眼能看到 LLM 抽出来什么。"""
    lines = ["已记录 ✓"]
    # 只展示这几个关键字段，避免回复太长
    for key, label in [
        ("project_name", "Project"),
        ("valuation", "Valuation"),
        ("category", "Category"),
        ("round", "Round"),
    ]:
        val = (parsed.get(key) or "").strip()
        if val:
            lines.append(f"{label}: {val}")
    return "\n".join(lines)


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
        ws.update(values=[["key", "value"]], range_name="A1:B1")
        ws.update(values=[["last_update_id", "0"]], range_name="A2:B2")
    if DATA_TAB not in titles:
        ws = sheet.add_worksheet(title=DATA_TAB, rows=1000, cols=4 + len(FIELD_DISPLAY_NAMES) + 5)
        header = ["Time", "Sender", "Message", "message_id"] + FIELD_DISPLAY_NAMES
        ws.update(values=[header], range_name="A1")


def get_last_update_id(sheet) -> int:
    val = sheet.worksheet(STATE_TAB).acell("B2").value
    try:
        return int(val) if val else 0
    except ValueError:
        return 0


def set_last_update_id(sheet, value: int):
    sheet.worksheet(STATE_TAB).update(values=[[str(value)]], range_name="B2")


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
    confirmations = []  # 并行存 (chat_id, message_id, parsed) 用于写完 Sheet 后发回复
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
            original = (original + f"\n[Attachment: {doc.get('file_name', 'unnamed')}]").strip()

        row = [ts, sender, original, message_id] + [parsed.get(k, "") for k in FIELD_KEYS]
        rows.append(row)
        confirmations.append((chat_id, message_id, parsed))

    if rows:
        # 先写 Sheet —— 失败的话异常会抛出，下面回复就不会发，下次 cron 自动重试
        append_rows(sheet, rows)
        print(f"[info] appended {len(rows)} rows")

        # Sheet 写入成功之后再发"已记录"回复，每条原消息一条 reply
        for chat_id, msg_id, parsed in confirmations:
            text = build_confirmation_text(parsed)
            send_telegram_reply(chat_id, msg_id, text)
        print(f"[info] sent {len(confirmations)} confirmation replies")
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
