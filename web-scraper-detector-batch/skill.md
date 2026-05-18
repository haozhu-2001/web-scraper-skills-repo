---
name: web-scraper-detector-batch
description: 批量网站抓取方式检测，读取飞书多维表格中的 URL 列表，逐行以独立 Agent 子上下文执行检测并立即写回结果。
---

# 批量网站抓取方式检测器

从飞书多维表格读取 URL 列表，逐行为每条 URL 启动独立 Agent 子上下文执行检测和抓取配置，**处理完一条立即写回一条**。禁止攒到最后批量写回。

---

## 路由判定（最先执行）

根据用户输入判定走哪条路径，**二者互斥，不可混淆**：

| 用户输入 | 执行路径 |
|----------|----------|
| 单个 URL（`http://` 或 `https://` 开头，不含飞书域名） | 启动独立 Agent 执行检测，返回结果原样输出，不走任何表格操作 |
| 飞书多维表格链接（含 `feishu.cn`/`larkoffice.com` 域名的表格） | 执行下方 B1→B2→B3 完整批量流程 |

**单 URL 路径——启动独立 Agent：**

```
Agent(
  subagent_type="general-purpose",
  description="Detect scraper method for URL",
  prompt="严格按以下步骤执行：
1. 调用 Skill 工具：skill='web-scraper-detector', args='<用户提供的URL>'
2. 将 Skill 返回的统一 JSON 作为你的唯一回复输出，不附加任何其他内容。"
)
```

Agent 返回后，将其输出的 JSON 原样展示给用户。流程结束。

**批量路径**：继续执行 B1。

---

## 前置条件

用户提供飞书多维表格链接，表格需包含以下列：

| 列 | 用途 |
|----|------|
| URL 列 | 待检测的 URL（输入） |
| 结果列 | 回填 `普通抓取` / `playwright抓取` / `配置失败` / `页面异常` / `检测失败` |
| DSL规则列 | 回填 DSL 配置 JSON |
| 备注列 | 回填检测摘要 |
| 修改时间列 | 回填写入时间戳 |
| 配置成功列 | 回填 `是` / `否` |

---

## 执行流程

### B1：读取表格结构

```bash
lark-cli base +field-list \
  --base-token <base_token> \
  --table-id <table_id> \
  --as user
```

从输出中确认以下列的字段 ID：URL 列、结果列、DSL规则列、备注列、修改时间列、配置成功列。

### B2：逐行处理（核心步骤）

每条 URL 启动一个独立 Agent 子上下文执行检测，Agent 返回后上下文自动释放。写回成功后立即丢弃本条 Agent 返回数据，仅保留计数器。

**B2 开始前初始化计数器：**
```
count_normal = 0
count_playwright = 0
count_config_fail = 0
count_page_error = 0
count_detect_fail = 0
count_skip = 0
```

**逐行处理循环：**
```
逐页读取记录（--limit 200，--offset 0, 200, 400...）：
  FOR EACH 当前页的记录（按 record_id 顺序）：
    1. 提取 URL 字段值
    2. IF URL 为空 → count_skip += 1，SKIP 本条，不写回，继续下一条
    3. 调用 Agent 工具：
       subagent_type = "general-purpose"
       description = "Detect scraper method for URL"
       prompt = "严格按以下步骤执行：
                1. 调用 Skill 工具：skill='web-scraper-detector', args='<URL>'
                2. 将 Skill 返回的统一 JSON 作为你的唯一回复输出，不附加任何其他内容。"
    4. 检查 Agent 是否成功返回：
       ├── Agent 返回正常 → 从输出中提取统一 JSON
       │     - 尝试直接解析为 JSON
       │     - 如果输出含 markdown 包裹，提取 ```json ... ``` 块
       │     - 如果仍无法解析 → 视为 Agent 失败
       └── Agent 超时/报错/返回非 JSON → 跳过步骤 5，直接执行 Agent 失败写回
    5. 从 JSON 中提取回填值（见下方字段映射表），确定各列内容
    6. 获取当前时间：NOW=$(python references/get_timestamp.py)
    7. 立即执行 lark-cli +record-upsert 写回本条记录
    8. 写回成功后：
       a. 根据 verdict 更新对应计数器（见下方计数器更新规则）
       b. 丢弃本条记录的 Agent 返回 JSON（已持久化到多维表格，无需保留）
       c. 处理下一条记录
  IF has_more → 读取下一页
```

**计数器更新规则（按条件判定，非单纯按 verdict 字符串匹配）：**
| 判定条件 | 计数器 |
|----------|--------|
| verdict = `普通抓取` 且 sub_skill 未返回 scrapable=false | `count_normal += 1` |
| verdict = `playwright抓取` 且 sub_skill 未返回 scrapable=false | `count_playwright += 1` |
| sub_skill.output.scrapable = false，或 sub_skill.error 不为 null（不论 verdict） | `count_config_fail += 1` |
| verdict = `页面异常` | `count_page_error += 1` |
| verdict = `检测失败` | `count_detect_fail += 1` |
| Agent 超时/失败（未返回有效 JSON） | `count_detect_fail += 1` |

> 注意：`配置失败` 不是 verdict 值。它来源于 sub_skill 返回的 `scrapable=false` 或 `sub_skill.error != null`。此时结果列填"配置失败"但计数器逻辑按上表第二条判定。

**Agent 失败写回（不调用步骤 5，直接写回）：**
```bash
NOW=$(python references/get_timestamp.py)

python -c "
import json
payload = {
    '<结果字段ID>': '检测失败',
    '<备注字段ID>': 'Agent调用失败：<超时/报错/非JSON>',
    '<时间字段ID>': '$NOW',
    '<配置成功字段ID>': '否'
}
with open('/tmp/lark_payload.json', 'w', encoding='utf-8') as f:
    json.dump(payload, f, ensure_ascii=False)
"

lark-cli base +record-upsert \
  --base-token <base_token> \
  --table-id <table_id> \
  --record-id <record_id> \
  --json "$(cat /tmp/lark_payload.json)" \
  --as user

rm /tmp/lark_payload.json
```
Agent 失败写回后同样更新 `count_detect_fail += 1`，丢弃返回数据，继续下一条。

**处理完一条记录的标志**：`+record-upsert` 已执行且返回成功 + 计数器已更新 + Agent 返回数据已释放。三个条件全部满足才能处理下一条。

**为什么用 Agent 而不是 Skill**：Agent 每次调用启动独立上下文，执行完自动销毁。每条 URL 的检测数据（HTTP 响应、浏览器 eval、DSL 模板）不会残留在主 context 中。写回后立即丢弃返回 JSON，进一步避免 context 膨胀。

### B3：输出汇总

所有记录处理并写回完成后，读取 B2 计数器输出汇总表（不涉及任何写回操作）。

---

## 子 Agent 调用规范

### 调用方式

对每条 URL，使用 **Agent** 工具（非 Skill 工具）启动独立子上下文：

```
Agent(
  subagent_type="general-purpose",
  description="Detect scraper method for URL",
  prompt="严格按以下步骤执行：
1. 调用 Skill 工具：skill='web-scraper-detector', args='<URL>'
2. 将 Skill 返回的统一 JSON 作为你的唯一回复输出，不附加任何其他内容。"
)
```

Agent 在独立上下文中启动，内部通过 `Skill("web-scraper-detector")` 加载检测指令并执行完整 6 步流程（HTTP → 浏览器 → 对比 → 判定 → 子 skill 配置 → 输出 JSON）。Agent 返回 JSON 后上下文自动释放。

### Agent 返回的 JSON 格式

```json
{
  "url": "<URL>",
  "detection": { ... },
  "verdict": "普通抓取|playwright抓取|页面异常|检测失败",
  "sub_skill": {
    "name": "static-scraper-config|dynamic-scraper-config|null",
    "called": true|false,
    "output": {
      "scrapable": true|false,
      "method": "普通抓取|playwright抓取|null",
      "rule": "默认规则|xpath规则|widget规则|null",
      "config": { ... },
      "reason": "错误原因|null"
    },
    "error": "<失败原因|null>"
  }
}
```

**sub_skill 字段解读：**
- `called=true, error=null` → 子 skill 调用成功，`output` 为有效数据
- `called=false, error="超时"` → 子 skill 调用失败，`output` 为 null，视为"配置失败"写回
- `called=false`（verdict 为 页面异常/检测失败时）→ 跳过子 skill，正常写回

### JSON 提取规则

Agent 输出可能被 markdown 包裹。按以下顺序尝试提取：
1. 直接作为 JSON 解析
2. 搜索 ` ```json ... ``` ` 代码块并提取内容
3. 搜索 `{` 开头、`}` 结尾的最外层 JSON 对象
4. 以上均失败 → 视为 Agent 失败，走 Agent 失败写回分支

### 字段映射表（返回 JSON → 多维表格列）

**按 verdict 分支处理：**

#### verdict = "普通抓取" 或 "playwright抓取"

从 `sub_skill.output` 提取：

| 表格列 | 值来源 | 说明 |
|--------|--------|------|
| 结果列 | `sub_skill.output.method` | 直接填入 |
| DSL规则列 | 见下方回填速查 | — |
| 备注列 | 见下方规则 | — |
| 修改时间列 | `python references/get_timestamp.py` | 实时获取 |
| 配置成功列 | `sub_skill.output.scrapable` | `true`→`是`，`false`→`否` |

**DSL规则列回填速查：**

| method | rule | config 值 | DSL规则列填入 |
|--------|------|-----------|-------------|
| 普通抓取 | 默认规则 | `null` | `"默认规则"` 字符串 |
| 普通抓取 | xpath规则 | JSON 对象 | `sub_skill.output.config` JSON 序列化为字符串后填入 |
| 普通抓取 | widget规则 | JSON 对象 | `sub_skill.output.config` JSON 序列化为字符串后填入 |
| playwright抓取 | xpath规则 | JSON 对象 | `sub_skill.output.config` JSON 序列化为字符串后填入 |

> **重要**：当 `config` 为 `null` 时（默认规则），DSL规则列填 `"默认规则"` 纯文本字符串，不填 `null` 或空串。config 为 JSON 对象时，需先 `json.dumps(config, ensure_ascii=False)` 序列化为字符串，再嵌入写回 payload。

**备注列格式**：`"web-scraper-detector→<sub_skill.name>，rule=<rule>，<检测关键数据>"`

示例：
- `"web-scraper-detector→static-scraper-config，rule=默认规则，HTTP200，173KB"`
- `"web-scraper-detector→dynamic-scraper-config，rule=xpath规则，list_item=//div[@class='item']，stop=3"`

#### verdict = "配置失败"（scrapable=false）

| 表格列 | 值 |
|--------|-----|
| 结果列 | `配置失败` |
| DSL规则列 | 留空 |
| 备注列 | `"web-scraper-detector→<sub_skill.name>，<sub_skill.output.reason>"` |
| 修改时间列 | `python references/get_timestamp.py` |
| 配置成功列 | `否` |

#### verdict = "页面异常"

| 表格列 | 值 |
|--------|-----|
| 结果列 | `页面异常` |
| DSL规则列 | 留空 |
| 备注列 | `"web-scraper-detector，页面异常，<HTTP状态>"` |
| 修改时间列 | `python references/get_timestamp.py` |
| 配置成功列 | `否` |

#### verdict = "检测失败"

| 表格列 | 值 |
|--------|-----|
| 结果列 | `检测失败` |
| DSL规则列 | 留空 |
| 备注列 | `"web-scraper-detector，检测失败，<失败原因>"` |
| 修改时间列 | `python references/get_timestamp.py` |
| 配置成功列 | `否` |

---

## 写回命令模板

**核心原则：所有写回使用 Python 构造 JSON 并写入临时文件，再由 lark-cli 读取，避免 shell 转义问题。禁止在 `--json` 中直接内联拼接 JSON 字符串（config JSON 含嵌套引号会导致 shell 解析失败）。**

### 通用写回流程

```bash
NOW=$(python references/get_timestamp.py)

# 用 Python 构造最终 payload JSON，写入临时文件
python -c "
import json

# === 以下值由 B2 步骤 5~6 的字段映射结果填入 ===
result_val = '<结果列值>'        # 普通抓取 / playwright抓取 / 配置失败 / 页面异常 / 检测失败
dsl_val = '<DSL规则列值>'        # config JSON 字符串 / '默认规则' / 空字符串
note_val = '<备注列值>'
success_val = '<是/否>'
# ================================================

payload = {
    '<结果字段ID>': result_val,
    '<DSL字段ID>': dsl_val,
    '<备注字段ID>': note_val,
    '<时间字段ID>': '$NOW',
    '<配置成功字段ID>': success_val
}
with open('/tmp/lark_payload.json', 'w', encoding='utf-8') as f:
    json.dump(payload, f, ensure_ascii=False)
"

lark-cli base +record-upsert \
  --base-token <base_token> \
  --table-id <table_id> \
  --record-id <record_id> \
  --json "$(cat /tmp/lark_payload.json)" \
  --as user

rm /tmp/lark_payload.json
```

### 各场景的变量赋值

**场景 A — 普通抓取/playwright抓取，scrapable=true：**

```python
result_val = '<sub_skill.output.method>'                    # "普通抓取" 或 "playwright抓取"
dsl_val = '<config JSON 序列化字符串 或 "默认规则">'         # 见 DSL回填速查表
note_val = 'web-scraper-detector→<sub_skill.name>，rule=<rule>，<检测摘要>'
success_val = '是'
```

**场景 B — 普通抓取/playwright抓取，scrapable=false：**

```python
result_val = '配置失败'
dsl_val = ''                                                 # 留空
note_val = 'web-scraper-detector→<sub_skill.name>，<sub_skill.output.reason>'
success_val = '否'
```

**场景 C — 页面异常（无配置产出）：**

```python
result_val = '页面异常'
dsl_val = ''                                                 # 留空
note_val = 'web-scraper-detector，页面异常，<HTTP状态码>'
success_val = '否'
```

**场景 D — 检测失败 / Agent 失败（无配置产出）：**

```python
result_val = '检测失败'
dsl_val = ''                                                 # 留空
note_val = 'web-scraper-detector，检测失败，<失败原因>'
success_val = '否'
```

### DSL 字段序列化规则

写入 `dsl_val` 前，按以下规则处理：

```python
import json

config = <sub_skill.output.config>   # 可能为 dict、None 或已是字符串

if config is None:
    dsl_val = '默认规则'
elif isinstance(config, dict):
    dsl_val = json.dumps(config, ensure_ascii=False)   # dict → JSON 字符串
else:
    dsl_val = str(config)                               # 已是字符串则直接用
```

---

## 汇总输出格式

所有记录处理并写回完成后输出：

```
## 批量检测完成 — Base: <base_url>

| 判定结果 | 数量 |
|----------|------|
| 普通抓取 | {count_normal} |
| playwright抓取 | {count_playwright} |
| 配置失败 | {count_config_fail} |
| 页面异常 | {count_page_error} |
| 检测失败 | {count_detect_fail} |
| 跳过（URL为空） | {count_skip} |

| 配置成功 | 数量 |
|----------|------|
| 是 | {count_normal + count_playwright} |
| 否 | {count_config_fail + count_page_error + count_detect_fail} |

共处理 {count_normal + count_playwright + count_config_fail + count_page_error + count_detect_fail} 条。
```

---

## 核心规则

1. 逐行处理，逐行写回。**+record-upsert 返回成功后才能处理下一条。**
2. 每条 URL 必须通过 **Agent** 工具（subagent_type="general-purpose"）以独立上下文执行检测，**禁止用 Skill 工具替代，禁止推测或跳过 Agent 调用。**
3. 写回成功后必须：更新计数器 → 丢弃本条 Agent 返回 JSON → 处理下一条。**禁止在 context 中保留历史 Agent 返回数据。**
4. Agent 超时/报错/返回非 JSON 时走"Agent 失败写回"分支，不可中断整个批量流程。
5. 所有计数操作基于 B2 初始化的 6 个计数器变量，B3 汇总直接读取。
6. 写回时只写存储字段，不写公式/查找/系统字段。
7. `--limit` 最大 200。分页串行执行。
8. 所有 Base 操作默认 `--as user`。权限错误走 lark-shared 再兜底 `--as bot`。
9. URL 为空→跳过，不写回。
10. browser-use 完全无法启动→中止批量运行。
11. Bitable API `91403` 不重试。`1254104` 减小 `--limit`。
12. 响应含 `ignored_fields`/`READONLY`→移除只读字段后重试。
13. **Agent 返回的 config JSON 原样填入 DSL规则列，禁止修改或截断。**

---

## 清理

全部写回完成后 `browser-use close`，删除临时目录。
