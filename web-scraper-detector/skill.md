---
name: web-scraper-detector
description: 网站抓取方式检测器，判断目标网站适合普通 HTTP 抓取还是需要 Playwright 浏览器渲染抓取。
---

# 网站抓取方式检测器

判断一个 URL 适合普通 HTTP 抓取还是 Playwright 浏览器渲染抓取，随后自动调用对应的子 skill 完成抓取规则配置，最终整合结果并输出总结。

两种模式：
- **单个 URL 模式**：用户给一个 URL，完成检测 → 子 skill 配置 → 整合输出。
- **多维表格批量模式**：用户给飞书多维表格链接，逐行读取 → 逐行处理（与单条 URL 流程一致）→ **处理完一行立即写回一行**（禁止攒到最后批量写回）。

## 何时使用

- 用户问"这个网站能用 HTTP/curl 抓吗？"
- 用户想知道网站是否有反爬保护。
- 用户提供多维表格链接并要求填写"普通抓取/playwright"列。
- 用户需要批量分类存储在飞书表格中的 URL 列表。

---

## 整体流程

### 单条 URL 流程

```
输入 URL
  → 第一步：HTTP GET 请求
  → 第二步：浏览器渲染（间隔 3 秒）
  → 第三步：对比 HTTP vs 浏览器指标
  → 第四步：判定抓取方式
  → 第五步：根据判定调用子 skill
      ├── 普通抓取 → static-scraper-config → 返回统一 JSON
      │     ├── scrapable=true  → 按映射回填各列
      │     └── scrapable=false → 结果列填"配置失败"
      ├── playwright抓取 → dynamic-scraper-config → 返回统一 JSON
      │     ├── scrapable=true  → 按映射回填各列
      │     └── scrapable=false → 结果列填"配置失败"
      ├── 页面异常 → 跳过（直接输出结果）
      └── 检测失败 → 跳过（直接输出结果）
  → 第六步：整合子 skill 输出，给出最终总结
```

### 多维表格批量流程

```
输入多维表格链接
  → B1：用 lark-cli 读取表格结构（+field-list）
  → B2：逐页读取记录（+record-list，--limit 200）
      → 逐行处理每条记录：
          1. 提取 URL
          2. 执行单条 URL 流程（检测 → 判定 → 强制调用子 skill → 整合结果）
          3. 🔴 立即写回当前这条记录（禁止攒到后面批量写回）
          4. 处理下一条
  → B3：输出批量处理汇总（仅报告，不再写回）
```

---

## Part A：单个 URL 检测工作流

### 第一步：HTTP GET 请求（必须先执行）

用真实浏览器 User-Agent 发起普通 GET 请求：

```bash
HTTP_CODE=$(curl -s -o "$TEMP/http_response.html" -w "%{http_code}" \
  --max-time 15 \
  --max-redirs 5 \
  -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36" \
  -H "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8" \
  -H "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8" \
  -H "Accept-Encoding: identity" \
  "$URL")
echo "HTTP_CODE=$HTTP_CODE"
```

然后使用以下命令提取关键指标（不要用 `grep -P`——Windows GBK 环境下不支持 Perl 正则）：

```bash
# 内容长度
wc -c < "$TEMP/http_response.html"

# 标题
LC_ALL=C grep -oiE '<title[^>]*>[^<]+</title>' "$TEMP/http_response.html" | head -1

# 脚本数量
LC_ALL=C grep -ci '<script' "$TEMP/http_response.html"

# 反爬关键词检测
LC_ALL=C grep -oicE 'captcha|challenge|access.denied|cf.browser|incapsula|验证码|Cloudflare.protection' "$TEMP/http_response.html" || echo 0

# 有效链接数：<a href="http..."> 且文本长度 >= 5
LC_ALL=C grep -oiE '<a [^>]*href="http[^"]*"[^>]*>[^<]*(<[^/][^>]*>[^<]*)*</a>' "$TEMP/http_response.html" \
  | sed 's/<[^>]*>//g' | LC_ALL=C sed 's/^[[:space:]]*//;s/[[:space:]]*$//' \
  | awk 'length >= 5' | wc -l

# 前 3 条链接文本（抽查）
LC_ALL=C grep -oiE '<a [^>]*href="http[^"]*"[^>]*>[^<]*(<[^/][^>]*>[^<]*)*</a>' "$TEMP/http_response.html" \
  | sed 's/<[^>]*>//g' | LC_ALL=C sed 's/^[[:space:]]*//;s/[[:space:]]*$//' \
  | awk 'length >= 5' | head -3
```

记录以下指标：
- `http_code`：HTTP 状态码（curl 出错/超时则为 0）
- `content_length_http`：字节数
- `title_http`：页面标题
- `script_count_http`：`<script>` 标签数量
- `anti_bot_http`：>0 表示发现反爬关键词
- `meaningful_link_count_http`：带 `href` URL 且文本 ≥ 5 个字符的 `<a>` 标签数量
- `first_link_texts_http`：前 3 条链接文本

### 第二步：浏览器 HTML（必须在第一步之后运行，间隔 3 秒以上）

```bash
sleep 3
browser-use open "$URL"
```

逐个提取指标，不要直接 dump 完整 HTML：

```bash
browser-use get title
browser-use eval "document.querySelectorAll('script').length"
browser-use eval "document.documentElement.outerHTML.length"
browser-use eval "document.body.innerText.includes('captcha') || document.body.innerText.includes('challenge') || document.body.innerText.includes('Cloudflare')"
browser-use eval "Array.from(document.querySelectorAll('a[href]')).filter(function(a){var t=a.textContent.trim();return /^https?:/i.test(a.href)&&t.length>=5}).length"
browser-use eval "Array.from(document.querySelectorAll('a[href]')).filter(function(a){var t=a.textContent.trim();return /^https?:/i.test(a.href)&&t.length>=5}).slice(0,3).map(function(a){return a.textContent.trim()}).join(' ||| ')"
```

> **为什么逐条 eval：** `browser-use get html` 在某些版本会崩溃；`browser-use state` 在 Windows GBK 下会触发 `UnicodeEncodeError`；`eval` 获取标量值速度快且不会被截断。

提取完成后关闭浏览器：
```bash
browser-use close
```

### 第三步：对比

| 维度 | HTTP 来源 | 浏览器来源 | 可疑情况 |
|------|----------|----------|----------|
| **HTTP 状态** | `http_code` | 页面是否加载？ | 状态类别不一致 |
| **标题** | 从文件 grep | `browser-use get title` | 变为 "Just a moment..." / "Attention Required!" / "安全检查" |
| **内容长度** | `wc -c` | `.outerHTML.length` | 差异 > 30% |
| **脚本数量** | `grep -c <script` | `querySelectorAll('script').length` | 浏览器侧明显更高 |
| **反爬关键词** | 从文件 grep | body 文本检查 | 任一侧出现：`captcha`、`challenge`、`access denied`、`cf-browser-verify`、`incapsula`、`验证码` |
| **有效链接**（文本 ≥ 5 字符） | HTTP grep 计数 | 浏览器 eval 计数 | HTTP = 0 但浏览器有大量链接 → JS 渲染内容 |

### 第四步：判定抓取方式

根据第三步的对比结果，按下表逐条匹配，**有且仅有一个判定结果**：

| 场景 | 判定 |
|------|------|
| 所有维度一致 + 无反爬 + 差异 < 10% | **普通抓取** |
| HTTP 有效链接 = 0，浏览器有完整链接 | **playwright抓取** |
| 标题变为 challenge/captcha 页面 + 出现反爬关键词 | **playwright抓取** |
| HTTP 返回 403/503 + 浏览器正常 | **playwright抓取** |
| 内容长度差异 > 30% + 浏览器脚本数量明显更多 | **playwright抓取** |
| HTTP 超时/连接失败 + 浏览器正常 | **playwright抓取** |
| HTTP 返回 404/410/50x 且浏览器也无法正常加载页面内容 | **页面异常** |
| HTTP 和浏览器均失败（超时、连接失败等网络层问题） | **检测失败** |

### 第五步：强制调用子 Skill

> **此步骤不可跳过。判定完成后必须立即调用对应子 skill，拿到子 skill 返回结果后才能进入第六步。**

**判定结果 → 子 skill 映射（一一对应，无例外）：**

| 判定结果 | 必须调用的子 skill | 说明 |
|----------|-------------------|------|
| **普通抓取** | `static-scraper-config` | HTTP 可直接提取内容，走静态抓取配置 |
| **playwright抓取** | `dynamic-scraper-config` | 需要浏览器渲染，走动态抓取配置 |
| **页面异常** | 无（不调用子 skill） | HTTP 和浏览器均返回 404/410/50x，页面本身不可达，无需配置抓取规则 |
| **检测失败** | 无（不调用子 skill） | HTTP 和浏览器均网络层失败，无需配置 |

**子 skill 统一返回格式：**

两个子 skill 均返回以下 JSON 结构：

```json
// scrapable=true 时：
{
  "scrapable": true,
  "method": "普通抓取" | "playwright抓取",
  "rule": "默认规则" | "xpath规则" | "widget规则",
  "config": { /* DSL 配置 JSON */ }
}

// scrapable=false 时：
{
  "scrapable": false,
  "method": null,
  "rule": null,
  "reason": "XPath不可提取：<具体原因>"
}
```

**返回字段 → 多维表格列映射（强制，不得自行变更）：**

| 返回字段 | 映射到多维表格列 | 映射规则 |
|----------|-----------------|----------|
| `scrapable` | 配置成功列 | `true` → 配置成功列填"是"；`false` → 配置成功列填"否"，结果列填"配置失败"，DSL规则列留空，备注列填入子 skill 名称 + `reason` 字段 |
| `method` | 结果列 | 直接映射：`"普通抓取"` → 结果列填 `"普通抓取"`；`"playwright抓取"` → 结果列填 `"playwright抓取"` |
| `rule` | 备注列（部分） | 作为备注列摘要的一部分，如 `"rule=默认规则"` / `"rule=xpath规则"` |
| `config` | DSL规则列 | 见下方 `config` 字段回填规则 |

**`config` 字段回填规则（按场景）：**

| 场景 | DSL规则列填入内容 |
|------|------------------|
| `method="普通抓取"` + `rule="默认规则"` | `rule` 字符串（如 `"默认规则"`），因无 DSL config |
| `method="普通抓取"` + `rule="xpath规则"` 或 `"widget规则"` | `config` JSON（op_list 格式） |
| `method="playwright抓取"` | `config` JSON（entry DSL 格式） |
| `scrapable=false` | 留空 |

#### 5.1 调用前：必须向用户输出调用声明

**在调用 Skill 工具之前，必须先在对话中输出以下文本（不可省略）：**

```
🔧 正在调用子 skill: <static-scraper-config / dynamic-scraper-config>
   判定结果: <普通抓取 / playwright抓取>
   传给子 skill 的信息: <URL + 检测结论摘要>
```

#### 5.2 执行调用

使用 `Skill` 工具，`skill` 参数传子 skill 名称，`args` 参数传 URL 和检测结论摘要。

**必须实际触发工具调用（工具调用记录中可见），禁止只在文本中说"已调用"。**

#### 5.3 调用后：必须原样输出子 skill 返回结果

**子 skill 返回后，必须在对话中输出以下内容（不可折叠、不可省略、不可摘要）：**

```
📋 子 skill <skill_name> 返回结果（完整原文，未折叠）：
<此处原样粘贴子 skill 返回的全部内容，一字不改>
```

> **为什么必须原样输出？** 子 skill（尤其是 `dynamic-scraper-config`）的返回结果是完整的 DSL JSON，需要原样回填到多维表格 DSL规则列。任何摘要、折叠、省略都会导致回填内容不完整或错误。

#### 5.4 自检清单（每条 URL 处理完后必须逐项确认，全部为"是"才能进入第六步）

- [ ] 第四步得出了判定结果
- [ ] **已向用户输出"🔧 正在调用子 skill"声明**（含 skill 名称、URL、判定结果）
- [ ] **已实际调用 Skill 工具**（检查本轮对话的工具调用记录，确认 Skill 工具确实被调用了）
- [ ] **已向用户输出"📋 子 skill 返回结果（完整原文）"**（子 skill 返回内容无折叠、无省略）
- [ ] 如果判定是"普通抓取"或"playwright抓取"但 Skill 工具未实际调用 → **立即补调用，不得跳过**
- [ ] 如果子 skill 返回了错误 → 记录错误信息，备注列体现错误，配置成功填"否"

### 第六步：整合子 Skill 输出并给出最终总结

**本步在收到子 skill 返回结果后执行。** 解析子 skill 返回的统一 JSON 格式，提取各字段按映射规则回填。

**子 skill 返回格式（两个子 skill 统一）：**

```json
{
  "scrapable": true,
  "method": "普通抓取",
  "rule": "默认规则",
  "config": { }
}
```

**字段解析与回填映射：**

```
子 skill 返回 JSON
  ├── scrapable:  true  → 按正常流程回填各列
  │              false → 结果列填"配置失败"，DSL规则列留空，配置成功列填"否"，
  │                      备注列填入子 skill 名称 + reason 字段
  ├── method:    结果列 ← 直接映射（"普通抓取" / "playwright抓取"）
  ├── rule:      备注列 ← 作为摘要的一部分（如 "rule=默认规则"）
  └── config:    DSL规则列 ← 按场景：
                  · 普通抓取+默认规则 → 填 rule 字符串（无 config）
                  · 普通抓取+xpath/widget → 填 config JSON（op_list）
                  · playwright抓取 → 填 config JSON（entry DSL）
```

整合输出：

```
## 检测总结 — URL: <url>

- 抓取方式：<method 字段值>
- scrapable：<true/false>
- rule：<rule 字段值>
- 子 skill 调用状态：已调用 <skill_name> / 未调用（原因：<页面异常/检测失败>）
- DSL规则（回填用）：<按 config 回填规则提取的值>
- 备注（回填用）：<子 skill 名称 + rule + 检测结论摘要>
```

---

## Part B：多维表格批量模式

### 工作流

#### B1. 获取表格结构

用 lark-cli 读取表格字段列表，确认各列名称和类型：

```bash
lark-cli base +field-list \
  --base-token <base_token> \
  --table-id <table_id> \
  --as user
```

映射字段：URL 列、结果列、备注列、DSL规则列、修改时间列、配置成功列。每次写回前通过 `python references/get_timestamp.py` 获取实时时间填入修改时间列。

#### B2. 逐行读取并处理（处理完一行立即写回一行）

> **核心原则：逐行处理，逐行写回。严禁把所有行的结果攒到最后一起写回——每处理完一条记录，必须立即写回多维表格，然后才能处理下一条。**

每一行的处理流程与单条 URL 完全一致。**禁止跳过子 skill 调用直接写回。**

```
循环处理每一页记录：
  读取一页（--limit 200，--offset 从 0 开始，然后 200、400...）
  对当前页的每条记录：
    1. 如果 URL 为空 → 跳过
    2. → 执行 Part A 第一步（HTTP 检测）
    3. → 执行 Part A 第二步（浏览器检测）
    4. → 执行 Part A 第三步（对比）
    5. → 执行 Part A 第四步（判定），输出判定结论
    6. → 🔧【强制可见步骤】向用户输出"正在调用子 skill: <名称>"声明
    7. → 🔧【强制工具调用】使用 Skill 工具实际调用子 skill（必须出现在工具调用记录中）
    8. → 📋【强制可见步骤】子 skill 返回后，用"📋 子 skill 返回结果（完整原文）："开头，原样输出子 skill 全部返回内容
    9. → 执行 Part A 第六步（解析子 skill 返回的统一 JSON，准备各列回填值）：
        - 结果列：取子 skill 返回的 `method` 字段直接映射（"普通抓取" / "playwright抓取"）
        - DSL规则列：按 config 回填规则提取（见下方 DSL规则列回填速查表）
        - 备注列：子 skill 名称 + `rule` 字段 + 返回结果简要提炼概括
        - 配置成功列：按下方"配置成功判定规则"确定值为"是"或"否"
        - scrapable=false 时：结果列填"配置失败"，DSL规则列留空，配置成功列填"否"，备注列填入子 skill 名称 + `reason` 字段

        **DSL规则列回填速查表：**
        | method | rule | DSL规则列填入 |
        |--------|------|-------------|
        | 普通抓取 | 默认规则 | `rule` 字符串 |
        | 普通抓取 | xpath规则 | `config` JSON |
        | 普通抓取 | widget规则 | `config` JSON |
        | playwright抓取 | xpath规则 | `config` JSON |
    10. → 获取当前时间，立即写回当前这条记录（见下方写回命令），写回成功后才能执行步骤 11
    11. → 处理下一条记录
  如果 has_more → 读取下一页
```

> **步骤 6/7/8 是防止子 skill 调用退化的三道关卡。** 步骤 6 迫使模型声明意图，步骤 7 迫使模型实际执行工具调用，步骤 8 迫使模型展示调用结果。三道关卡全部在对话中可见，无法伪造。跳过任一步骤即视为违规，必须回退补做。

**配置成功判定规则（步骤 7 中确定）：**

一条记录"配置成功=是"需同时满足以下条件，任一不满足则为"否"：

| 条件 | 要求 |
|------|------|
| 结果列已填 | 值必须为以下之一：`普通抓取`、`playwright抓取`、`配置失败`、`页面异常`、`检测失败` |
| DSL规则列正确 | 普通抓取/playwright抓取 → DSL规则列必须非空；配置失败/页面异常/检测失败 → DSL规则列留空即正确 |
| 备注列已填 | 非空，且包含子 skill 名称及返回结果的简要提炼概括（或跳过原因） |
| 修改时间列已填 | 非空 |

**步骤 8 写回命令（处理完一条记录后立即执行，不得延迟）：**

```bash
# 每条写回前先获取当前时间：
NOW=$(python references/get_timestamp.py)

# === 普通抓取（已调用 static-scraper-config）===
# 子 skill 返回格式：{"scrapable": true, "method": "普通抓取", "rule": "<rule>", "config": {...}}
# 结果列 ← method 字段；DSL规则列 ← 按回填速查表提取
# 默认规则：config 为 null，DSL规则列填 rule 字符串
lark-cli base +record-upsert \
  --base-token <base_token> \
  --table-id <table_id> \
  --record-id <record_id> \
  --json '{"<结果字段>":"<method>","<DSL字段>":"<rule字符串或config JSON>","<备注字段>":"已调用 static-scraper-config，rule=<rule>，<结果摘要>","<时间字段>":"'"$NOW"'","<配置成功字段>":"是"}' \
  --as user

# === playwright抓取（已调用 dynamic-scraper-config）===
# 子 skill 返回格式：{"scrapable": true, "method": "playwright抓取", "rule": "xpath规则", "config": {<entry DSL>}}
# 结果列 ← method 字段；DSL规则列 ← config JSON 原样填入
lark-cli base +record-upsert \
  --base-token <base_token> \
  --table-id <table_id> \
  --record-id <record_id> \
  --json '{"<结果字段>":"<method>","<DSL字段>":"<config JSON 原样>","<备注字段>":"已调用 dynamic-scraper-config，rule=xpath规则，<结果摘要>","<时间字段>":"'"$NOW"'","<配置成功字段>":"是"}' \
  --as user

# === 子 skill 返回 scrapable=false（XPath 不可提取，无法配置）===
# 子 skill 返回格式：{"scrapable": false, "method": null, "rule": null, "reason": "XPath不可提取：<具体原因>"}
# 结果列填"配置失败"，DSL规则列留空，配置成功列填"否"，备注列填入子 skill 名称 + reason 字段
lark-cli base +record-upsert \
  --base-token <base_token> \
  --table-id <table_id> \
  --record-id <record_id> \
  --json '{"<结果字段>":"配置失败","<备注字段>":"已调用 <skill_name>，<reason字段>","<时间字段>":"'"$NOW"'","<配置成功字段>":"否"}' \
  --as user

# === 检测失败（HTTP 和浏览器均网络层失败，不调用子 skill）===
# 结果列填"检测失败"，备注列填失败原因，DSL规则列留空
lark-cli base +record-upsert \
  --base-token <base_token> \
  --table-id <table_id> \
  --record-id <record_id> \
  --json '{"<结果字段>":"检测失败","<备注字段>":"未调用子skill，<失败原因>","<时间字段>":"'"$NOW"'","<配置成功字段>":"是"}' \
  --as user
```

**写回规则：**
- 🔴 **处理完一条，立即写回一条。** 禁止先收集所有结果再统一写回。
- 每条记录单独 `+record-upsert --record-id`，写回成功后才能处理下一条。
- 只写入存储字段，不要写入公式/查找/系统字段。
- **修改时间列必填**：每条写回前必须执行 `NOW=$(python references/get_timestamp.py)` 获取实时时间，填入修改时间列。
- **DSL规则列必填规则：**
  - 从子 skill 返回的统一 JSON 中提取，按 **DSL规则列回填速查表**（B2 步骤 9）回填。
  - 普通抓取 + 默认规则：`config` 为 null，填 `rule` 字符串。
  - 普通抓取 + xpath/widget 规则：填 `config` JSON（op_list 格式）。
  - playwright抓取：填 `config` JSON（entry DSL 格式），**原样填入不修改**。
  - `scrapable=false` / 配置失败 / 页面异常 / 检测失败：DSL规则列不填。
- **备注列必填规则：**
  - 普通抓取 / playwright抓取：备注列 = 调用的子 skill 名称 + `rule` 字段 + 返回结果的简要提炼概括（不是原文照抄）。示例：`"已调用 static-scraper-config，rule=默认规则，HTTP 200，标题一致，173KB，28个容器，101链接"` 或 `"已调用 dynamic-scraper-config，rule=xpath规则，list_item=//div[@class='item']，分页stop=3"`
  - 配置失败：备注列 = 子 skill 名称 + `reason` 字段（如 `"已调用 static-scraper-config，XPath不可提取：容器class不统一"`）
  - 页面异常：备注列包含 HTTP 状态码及浏览器加载情况的简要描述
  - 检测失败：备注列包含失败原因
- 配置失败的记录，结果列填 `"配置失败"`；页面异常的记录，结果列填 `"页面异常"`；检测失败的记录，结果列填 `"检测失败"`。
- **配置成功列必填规则：**
  - 符合以下全部条件时填 `"是"`，任一不满足则填 `"否"`：
    1. 结果列已填入（`普通抓取` / `playwright抓取` / `配置失败` / `页面异常` / `检测失败`）
    2. DSL规则列状态正确（普通抓取/playwright抓取 → 非空；配置失败/页面异常/检测失败 → 空是正确的）
    3. 备注列非空
    4. 修改时间列非空
  - 若子 skill 调用异常（如 static-scraper-config / dynamic-scraper-config 返回错误、超时），导致 DSL规则列为空或备注不完整，则填 `"否"`。

#### B3. 输出最终汇总

**仅在所有记录处理并写回完成后**，输出汇总报告（不涉及任何写回操作）。

#### B4. 报告汇总

**仅在所有记录逐条处理并逐条写回完成后**，输出汇总：

```
## 批量检测完成

| 结果 | 数量 |
|------|------|
| 普通抓取 | N |
| playwright抓取 | M |
| 配置失败 | L |
| 页面异常 | P |
| 检测失败 | K |

| 配置成功 | 数量 |
|----------|------|
| 是 | Y |
| 否 | Z |

Base: <base_url>
共更新 X 条记录。
```

### 身份与权限

- 所有 Base 操作默认使用 `--as user`。
- 如果用户身份返回权限错误，走 lark-shared 权限拒绝流程，再兜底到 `--as bot`。

### 跳过与异常处理

- URL 为空的记录直接跳过，不写回。
- `+record-list` 分页串行执行，`--limit` 最大 200。
- browser-use 完全无法启动（非单条 URL 问题）→ 中止批量运行并报告。
- Bitable API 返回 `91403`（无权限）→ 不重试，走 lark-shared 权限流程。
- Bitable API 返回 `1254104`（超过页大小限制）→ 减小 `--limit`。
- 响应中出现 `ignored_fields` / `READONLY` → 移除只读字段后重试写入。

---

## 核心规则

1. **第一步必须先于第二步执行。** 严禁并行或颠倒顺序。
2. **第二步使用逐条 eval 调用**，不要 dump 完整 HTML。
3. **同一域名 HTTP 和浏览器请求之间至少间隔 3 秒**（`sleep 3`）。
4. **HTTP 超时可重试一次。** 浏览器超时不要重试——直接记录失败。
5. **非 200 响应也要检测。** HTTP 返回 403/503 且有 body，仍需执行第二步。
6. **所有 grep 命令加 `LC_ALL=C` 前缀**——避免 Windows GBK locale 问题。
7. **用 `grep -E`，不要用 `grep -P`**——某些 Windows/MinGW 版本不支持 Perl 正则。
8. **严禁使用 `browser-use state`**——Windows GBK 下遇到 Unicode 内容会崩溃。
9. **严禁使用 `browser-use get html`**——已知存在 traceback bug。
10. **判定后根据结果决定是否调用子 skill，拿到结果后才能写回**：
    - 普通抓取 → 必须调用 `static-scraper-config`，解析返回的统一 JSON，按字段映射规则回填各列。若 `scrapable=false` → 结果列填"配置失败"，DSL规则列留空
    - playwright抓取 → 必须调用 `dynamic-scraper-config`，解析返回的统一 JSON，按字段映射规则回填各列。若 `scrapable=false` → 结果列填"配置失败"，DSL规则列留空
    - 页面异常 → 不调用子 skill，直接写回，备注列描述 HTTP 状态码和浏览器情况
    - 检测失败 → 不调用子 skill，直接写回，备注列描述失败原因
    - **严禁跳过子 skill 直接写回多维表格或输出结论（页面异常/检测失败除外）**
    - **子 skill 统一返回格式：** `{"scrapable": bool, "method": str, "rule": str, "config": {}, "reason": str}`，回填映射见 Part A 第六步
11. **批量模式下每条 URL 后必须 `browser-use close`**。
12. **子 skill 返回内容必须按统一格式解析并回填**：
    - 两个子 skill 均返回 `{"scrapable", "method", "rule", "config", "reason"}` 格式
    - `scrapable=true`：结果列 ← `method`；DSL规则列 ← 按回填速查表提取；备注列 ← 子 skill 名称 + `rule` + 摘要
    - `scrapable=false`：结果列 ← "配置失败"；DSL规则列 ← 留空；备注列 ← 子 skill 名称 + `reason` 字段；配置成功列 ← "否"
    - `config` JSON 回填时原样保留，不修改、不截断
13. **修改时间列每次写回前通过 `python references/get_timestamp.py` 获取实时时间填入**，格式 `yyyy-MM-dd HH:mm:ss`。
14. 🔴 **逐条处理，逐条写回**：读取 → 检测 → 子 skill → 整合 → **立即写回当前这条** → 下一条。**严禁将所有结果攒到最后批量写回。** 写回必须在处理下一条之前完成。
15. **最多 200 条每页**，`has_more` 时串行翻页。
16. **批量模式下，B4 汇总报告仅在所有记录处理并写回完成后输出**，汇总阶段不再写回。
17. **配置成功列判定规则**：一条记录的结果列、DSL规则列（按场景）、备注列、修改时间列全部正确填充时填 `"是"`，否则填 `"否"`。子 skill 调用失败导致规则或备注缺失也填 `"否"`。具体见 Part B 配置成功判定规则。
18. 🔴 **子 skill 调用声明规则（防退化）**：每次调用子 skill 前，必须在对话中输出 `🔧 正在调用子 skill: <skill_name>`，包含当前 URL、判定结果、子 skill 名称。**未输出此声明即进行后续步骤 = 违规，必须回退补做。**
19. 🔴 **子 skill 结果展示规则（防折叠）**：子 skill 返回后，必须用 `📋 子 skill <名称> 返回结果（完整原文）：` 开头，**原样输出子 skill 的全部返回内容**。禁止摘要、禁止折叠、禁止省略、禁止用"详见xxx"代替、禁止只在脑中记住结果而不输出。**不输出原文即写回 = 违规。**
20. **备注列内容规则**：备注列 = 子 skill 名称 + 返回结果的简要提炼概括（不是原文照抄）。例如 `"已调用 static-scraper-config，HTTP 200，标题一致，173KB，28个容器，101链接，rule=默认规则"`。页面异常/检测失败的备注描述原因即可。
21. 🔴 **批量模式序号规则**：批量模式下，每条 URL 处理前必须输出 `### [N/Total] <URL>` 作为该条处理的标题，方便追踪每条 URL 的子 skill 调用状态。标题中必须包含当前是第几条、总共多少条。

## 清理

- 单个 URL 模式：检测完成后 `browser-use close` + 删除临时目录。
- 批量模式：每条 URL 后 `browser-use close`；所有写入完成后删除批次临时目录。
