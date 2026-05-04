# Telegram 群消息 → Google Sheet 同步

每小时把 Telegram 群里 **@ 了 Bot** 的消息追加到 Google Sheet（如果消息里带有 `字段：值` 这类结构化内容会顺便解析到对应列）。

## 文件清单

```
.
├── telegram_to_sheet.py        # 主脚本
├── requirements.txt            # Python 依赖
├── .github/workflows/sync.yml  # GitHub Actions 定时配置
└── README.md                   # 本文件
```

## 触发规则

满足下列任一条件的群消息都会被记录：

1. 消息文本里出现 `@<你的BotUsername>`
2. 消息是「**回复**（reply）Bot 发的某条消息」
3. 用 text_mention 提及了 Bot（手动从联系人点选 Bot 的情况，少见）

不 @ Bot 的普通群聊不会被记录。

## 消息示例

最简单的用法 —— 自然地 @ 一下，整条消息会原样存进 `原始消息` 列：

```
@MyBot 张三今天来了，记一下
```

如果还想顺便填进结构化的列（`姓名` / `日期` / `金额` 等），把模板字段写在消息里：

```
@MyBot 麻烦录一下
姓名：张三
日期：2026-05-04
金额：1200
```

中文/英文冒号都行；不带 `字段：值` 的消息也会被记录，对应几列留空就是。

写入 Sheet 后的列：

```
时间 | 发送人 | 原始消息 | message_id | 姓名 | 日期 | 金额
```

要改字段，编辑 `telegram_to_sheet.py` 里的 `EXPECTED_FIELDS` 即可，表头会自动按这个列表生成。

---

## 一次性配置（大概 20 分钟）

### 1. 创建 Telegram Bot

1. 在 Telegram 里搜索 `@BotFather` → 发 `/newbot`
2. 按提示起名字，记下它给你的 **Bot Token**（形如 `1234567890:ABCDEF...`）和 **username**（形如 `MyBot`，下文 @ 它就用这个）
3. 把 Bot 拉进你的群（不一定要管理员，普通成员就行）

> Privacy mode 这次不用关——@ Bot 的消息和 reply Bot 的消息都会绕过隐私限制，Bot 默认就能收到。

### 2. 拿到群的 chat_id（可选但建议）

只有限定群，才能避免 Bot 万一被加到别的群也会乱写。

1. 在群里随便发一条消息
2. 浏览器打开（把 `<TOKEN>` 换成你的 Bot Token）：
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
3. 在返回的 JSON 里找 `"chat":{"id": -100xxxxx ...}`，那个数字（含负号）就是 `chat_id`

### 3. 准备 Google Sheet 写入权限

1. 打开 [Google Cloud Console](https://console.cloud.google.com/) → 新建一个项目（名字随意）
2. 左侧菜单 **APIs & Services → Library** → 搜 "Google Sheets API" → **Enable**
3. 左侧菜单 **APIs & Services → Credentials** → **Create Credentials → Service Account**
   - 名字随便取（比如 `tg-sync`），不用授权角色
   - 创建完点这个 Service Account → **Keys 标签 → Add Key → Create new key → JSON**
   - 浏览器会自动下载一个 JSON 文件，里面会有一个 `client_email`，形如 `tg-sync@xxx.iam.gserviceaccount.com`
4. 新建一个 Google Sheet（或用现有的），点右上角 **Share**，把上面那个 `client_email` 加为 **Editor**
5. 复制 Sheet 的 ID：URL `https://docs.google.com/spreadsheets/d/【这一段就是 Sheet ID】/edit` 中括号里那段

### 4. 把代码放到 GitHub

1. 在 GitHub 创建一个 **私有仓库**（公开也行，但 Actions 配置和日志都会公开）
2. 把这四个文件传上去（保持目录结构，特别是 `.github/workflows/sync.yml` 必须在这个路径下）
3. 进 GitHub 仓库的 **Settings → Secrets and variables → Actions → New repository secret**，加四个：

   | Secret 名字                       | 值                                                |
   | --------------------------------- | ------------------------------------------------- |
   | `TELEGRAM_BOT_TOKEN`              | BotFather 给的 Token                              |
   | `GOOGLE_SHEET_ID`                 | 第 3 步复制的 Sheet ID                            |
   | `GOOGLE_SERVICE_ACCOUNT_JSON`     | 第 3 步下载的 JSON 文件的**完整内容**（整个粘进去）|
   | `TELEGRAM_GROUP_CHAT_ID`          | 第 2 步的 chat_id（如果不需要限定可以留空不建）   |

### 5. 跑起来

1. 进仓库 **Actions** 标签页 → 找到 "Sync Telegram to Google Sheet"
2. 点 **Run workflow**（手动跑一次验证一下）
3. 看日志：成功的话会打印 `appended N rows` 或 `no #录入 messages this run`
4. 之后每小时整点（UTC）GitHub 会自动跑一次

---

## 验证

1. 在群里发一条 `@你的Bot username 测试一下` 这样的消息
2. 去 Actions 手动 **Run workflow**
3. 打开 Sheet，应该能看到一行新数据
4. 第一次跑完会自动建 `数据` 和 `_state` 两个 tab，不用手动建

## 常见问题

**没收到消息？**
- 你 @ 的 username 拼错了 / 大小写其实不分但要看清是不是同一个 Bot
- Bot 还没加进这个群
- 给 Bot 发的是普通文本而不是 @ 它（脚本只记录被 @ 的消息）

**报错 `the caller does not have permission`？**
Service Account 的邮箱没加到 Sheet 的协作者里，或者权限不是 Editor。

**Cron 不准时？**
GitHub Actions 的 cron 在高峰期会延迟几分钟到十几分钟，对小时级同步无感。

**想改成每 30 分钟跑一次？**
编辑 `sync.yml` 里 `cron: '0 * * * *'` → `cron: '*/30 * * * *'`（GitHub 最低支持 5 分钟一次）。

**消息漏掉了怎么办？**
脚本是按 `update_id` 增量拉取的。Telegram 服务端只保留 24 小时未确认的更新，所以只要 GitHub Actions 不连续 24 小时全部失败就不会丢。如果实在丢了，去 `_state` 这个 tab 把 `last_update_id` 改成更小的数字（甚至 0）就能让脚本重新拉。
