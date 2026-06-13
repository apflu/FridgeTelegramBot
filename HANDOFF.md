# 情况同步（给远程 / 后续接手）

> 更新于 2026-06-12。本文档同步本地这一轮改动的现状，**重点是价格/成本归账**。
> 旧背景看 [CLAUDE.md] 与代码注释；这里只记非显而易见的决策与部署注意事项。

## 一句话现状
冰箱 bot 已从「纯文字管理」扩展为「记账 + 保质期 + 到期提醒」。最近一轮主要在修**价格统计**：把"餐食成本"从采购流水解耦、改用消耗时的价格快照，并定下了 eat/finish 的成本归账规则。

---

## 价格 / 成本模型（最重要）

钱一律以**整数分**存储，展示时 `/100`。存在**三处**价格，互相解耦，别混：

| 数据 | 含义 | 来源 | 谁会清 |
|------|------|------|--------|
| `items.price_cents` | 每行库存（一个购买单位）的单价 | 收据导入写入 | 撤销本单 / 吃完移除 |
| `purchases.amount_cents` | **采购花费**（买入的钱，不可变流水） | 收据导入时记一笔 | 撤销本单、`/cleardebug` |
| `meal_items.price_cents` | **餐食成本**（吃掉东西的价值快照） | 见下方归账规则 | `/cleardebug` |

`/stats` 显示：**采购花费**（purchases 求和）、**餐食成本**（meal 快照求和）、餐数、平均每餐（= 餐食成本 ÷ 餐数）。两者是**两笔账**：采购 = 买了多少钱；餐食成本 = 吃掉多少钱。清了 purchases 不影响餐食成本。

### 成本归账规则（刚定，已落地）
一件商品 = 一行库存 = 一个价格，但可能被多顿饭分着吃。为避免「没吃完也计价」和「分两次吃算两遍」：

| 意图 | 库存 | 记入餐食 | 计入餐食成本 |
|------|------|---------|------|
| `eat`（吃了没吃完） | 保留 | ✅ | ❌ €0 |
| `finish`（吃完了） | **移除该行** | ✅ | ✅ **取被移除行的全价，仅一次** |
| `discard`（扔了/过期） | 移除 | 不算一餐 | ❌（浪费，不计） |

**关键**：成本只在「东西被吃光、离开冰箱」时归账，取被移除那一行的价；移除后无法再被 finish，故每行恰好计一次。代价：分次吃时成本整笔落在「吃完的那一餐」，不按口数摊分（不值得引入分数记账）；但总额永远正确、不重复。

实现位置：[fridgebot/bot/app.py] 的 `on_callback` 中 `eat/finish` 分支；统计在 [fridgebot/storage/db.py] `meal_cost_cents()` + [fridgebot/bot/render.py] `render_stats()`。

### ⚠️ 部署后必做：清一次旧数据
**部署本轮代码前记录的餐食是旧逻辑**（eat 也按全价记了）。新代码上线后：
- 新餐食正确；
- 但旧餐食里 eat 项仍是全价，且若那件东西之后又被「吃完」，会跨新旧逻辑**算两遍**。

**建议**：部署后用 `/cleardebug` 清一次（清 purchases + meals，**不动冰箱库存与单价**），从干净状态重记。数据量小，重置最省心。

---

## 本轮其它改动（远程可能还没有）
- **意图扩展**：`add / update / eat / finish / discard`（区分"吃了"与"吃完了"）。
- **保质期可空**：发票录入不推断保质期（留空），`/estimate` 命令对未知项批量 LLM 估算；空保质期不再被误判为"当天过期"，库存里显示 `⚪ 保质期未知`。
- **到期提醒**：每天一次，仅当有「今天到期或已过期」的项才发一条 DM；消息带 🔕 内联按钮可关。命令：`/due`（立即查）、`/mute` / `/unmute`（关/开）。开关存在 `kv.reminders_enabled`。
- **`/cleardebug`**：debug 命令，清空采购流水 + 餐食记录（总开销与餐数归零），**冰箱库存保留**。
- **结构化输出开关**：`LLM_STRUCTURED_OUTPUT`（见下）。prompt 已拆成「规则段（始终发）+ JSON 格式段（仅 json_object 模式追加）」，三处解析共用 `parser.complete()`。
- **多用户预留**：到期提醒的收件人集中在 `app.reminder_targets()`（现返回 `[(OWNER_ID, 全局db)]`）；将来每用户独立 DB 时只改这一处。开关/统计都按 DB 走，天然每用户独立。

---

## 部署注意
1. `uv sync`（依赖已从 google-genai 换为 openai）。
2. **DB 自动迁移**：`Database.connect()` 里 `_migrate()` 会给老 `fridge.db` 补列并重建 items 表去掉 expiry 的 NOT NULL（幂等、数据无损）。无需手动操作。
3. **新环境变量（都可选，看 [.env.example]）**：
   - `OPENAI_VISION_MODEL`：收据识别的视觉模型，留空回退 `OPENAI_MODEL`。
   - `CURRENCY`：展示币种，默认 `€`。
   - `LLM_STRUCTURED_OUTPUT`：`true`=json_schema 约束解码（Gemini/OpenAI 支持，对弱模型格式更稳）；空/`false`=json_object（端点兼容性最广）。本地 `.env` 当前为 `true`。
4. 运行：`uv run python -m fridgebot`
   - pm2：`pm2 start uv --name fridge-bot --cwd <项目目录> -- run python -m fridgebot`
   - `uv` 不在 PATH 时用绝对路径。

## 验证
- 离线（无 API，最有用）：`uv run python -m tests.test_db_batch` —— 覆盖 batch/价格/空保质期/购买流水/餐食/迁移。
- 在线（真实 API）：`uv run python -m tests.test_queue`、`tests.test_llm`。

## 当前未做（下一步候选）
- `/stats` 按日期区间（"这个月伙食费"）—— `spend_cents`/`meal_cost_cents`/`count_meals` 都已支持 start/end 参数，只差命令层接入。
- 分次吃的成本摊分（当前整笔落在吃完那餐，刻意从简）。
- 多用户（每用户独立 DB，扩展点见上）。
