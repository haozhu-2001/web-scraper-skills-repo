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
      ├── 普通抓取 → static-scraper-config
      ├── playwright抓取 → dynamic-scraper-config
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

| 判定结果 | 必须调用的子 skill | 返回格式 | 说明 |
|----------|-------------------|----------|------|
| **普通抓取** | `static-scraper-config` | 简单 JSON，含 `rule` 字符串字段 | HTTP 可直接提取内容，走静态抓取配置 |
| **playwright抓取** | `dynamic-scraper-config` | 完整 DSL JSON，原样填入 DSL规则列不修改 | 需要浏览器渲染，走动态抓取配置 |
| **页面异常** | 无（不调用子 skill） | — | HTTP 和浏览器均返回 404/410/50x，页面本身不可达，无需配置抓取规则 |
| **检测失败** | 无（不调用子 skill） | — | HTTP 和浏览器均网络层失败，无需配置 |

**调用方式**：使用 `Skill` 工具，`skill` 参数传子 skill 名称，`args` 参数传 URL 和检测结论摘要。

**自检清单（每条 URL 处理完后必须逐项确认）：**

- [ ] 第四步得出了判定结果（普通抓取 / playwright抓取 / 页面异常 / 检测失败）
- [ ] 如果判定是"普通抓取"或"playwright抓取"，已调用对应的子 skill（`static-scraper-config` 或 `dynamic-scraper-config`）
- [ ] 已收到子 skill 的返回结果
- [ ] 如果子 skill 未被调用，**立即补调用，不得跳过**

### 第六步：整合子 Skill 输出并给出最终总结

**本步在收到子 skill 返回结果后执行。** 整合输出：

```
## 检测总结

- URL: <url>
- 抓取方式：普通抓取 / playwright抓取 / 页面异常 / 检测失败
- DSL规则（回填用）：
    - 普通抓取：<子 skill 返回的 rule 字符串，如"默认规则"，原样回填>
    - playwright抓取：<子 skill 返回的完整 DSL JSON，原样填入不修改>
    - 页面异常/检测失败：留空
- 子 skill 处理结果：
  <子 skill 的输出摘要，若未调用子 skill 则写"未调用（原因）">
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

映射字段：URL 列、结果列、备注列、DSL规则列、修改时间列。每次写回前通过 `python references/get_timestamp.py` 获取实时时间填入修改时间列。

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
    5. → 执行 Part A 第四步（判定）
    6. → 执行 Part A 第五步（强制调用子 skill，仅"普通抓取"和"playwright抓取"调用；"页面异常"和"检测失败"跳过）
        ⚠️ 此步不可跳过。子 skill 未返回结果前，禁止进入步骤 7。
    7. → 执行 Part A 第六步（整合子 skill 结果，准备各列回填值）：
        - 结果列 / 备注列：自己根据子 skill 返回内容分析后填入
        - DSL规则列：
          · 普通抓取：直接提取子 skill 返回的 `rule` 字符串原样填入
          · playwright抓取：子 skill 返回完整 DSL JSON，原样填入不修改
          · 页面异常/检测失败：留空
    8. → 🔴 获取当前时间 `NOW=$(python references/get_timestamp.py)`，立即写回当前这条记录（见下方写回命令），写回成功后才能执行步骤 9
    9. → 处理下一条记录
  如果 has_more → 读取下一页
```

**步骤 8 写回命令（处理完一条记录后立即执行，不得延迟）：**

```bash
# 每条写回前先获取当前时间：
NOW=$(python references/get_timestamp.py)

# 普通抓取（已调用 static-scraper-config）：
# DSL规则列填入子 skill 返回的 rule 字符串，结果列和备注列由自己分析填入
lark-cli base +record-upsert \
  --base-token <base_token> \
  --table-id <table_id> \
  --record-id <record_id> \
  --json '{"<结果字段>":"普通抓取","<DSL字段>":"<子skill返回的rule字符串>","<备注字段>":"已调用 static-scraper-config，<结果摘要>","<时间字段>":"'"$NOW"'"}' \
  --as user

# playwright抓取（已调用 dynamic-scraper-config）：
# DSL规则列原样填入子 skill 返回的完整 DSL JSON（不修改），结果列和备注列由自己分析填入
lark-cli base +record-upsert \
  --base-token <base_token> \
  --table-id <table_id> \
  --record-id <record_id> \
  --json '{"<结果字段>":"playwright抓取","<DSL字段>":"<子skill返回的完整DSL JSON>","<备注字段>":"已调用 dynamic-scraper-config，<结果摘要>","<时间字段>":"'"$NOW"'"}' \
  --as user

# 页面异常（HTTP 和浏览器均返回 404/410/50x，不调用子 skill）：
lark-cli base +record-upsert \
  --base-token <base_token> \
  --table-id <table_id> \
  --record-id <record_id> \
  --json '{"<结果字段>":"页面异常","<备注字段>":"未调用子skill，<HTTP状态码及浏览器加载情况简述>","<时间字段>":"'"$NOW"'"}' \
  --as user

# 检测失败（HTTP 和浏览器均网络层失败，不调用子 skill）：
lark-cli base +record-upsert \
  --base-token <base_token> \
  --table-id <table_id> \
  --record-id <record_id> \
  --json '{"<结果字段>":"检测失败","<备注字段>":"未调用子skill，<失败原因>","<时间字段>":"'"$NOW"'"}' \
  --as user
```

**写回规则：**
- 🔴 **处理完一条，立即写回一条。** 禁止先收集所有结果再统一写回。
- 每条记录单独 `+record-upsert --record-id`，写回成功后才能处理下一条。
- 只写入存储字段，不要写入公式/查找/系统字段。
- **修改时间列必填**：每条写回前必须执行 `NOW=$(python references/get_timestamp.py)` 获取实时时间，填入修改时间列。
- **DSL规则列必填规则：**
  - 普通抓取：子 skill 返回简单 JSON（如 `{"scrapable": true, "rule": "默认规则"}`），直接提取 `rule` 字符串原样回填。
  - playwright抓取：子 skill 返回完整 DSL JSON，**原样填入不修改**。结果列和备注列需要自己分析子 skill 返回内容后填入。
  - 页面异常 / 检测失败：无子 skill 调用，DSL规则列不填。
- **备注列必填规则：**
  - 普通抓取 / playwright抓取：备注列必须包含调用的子 skill 名称（`static-scraper-config` / `dynamic-scraper-config`）及子 skill 返回结果的简要概括（自己分析总结）。
  - 页面异常：备注列必须包含 HTTP 状态码及浏览器加载情况的简要描述（如"HTTP 404，浏览器同样返回 404 页面"）。
  - 检测失败：备注列必须包含失败原因（如"HTTP 连接超时，浏览器也无法打开"）。
- 页面异常的记录，结果列填 `"页面异常"`；检测失败的记录，结果列填 `"检测失败"`。

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
| 页面异常 | P |
| 检测失败 | K |

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
    - 普通抓取 → 必须调用 `static-scraper-config`，直接提取返回的 `rule` 字符串回填 DSL规则列
    - playwright抓取 → 必须调用 `dynamic-scraper-config`，其返回完整 DSL JSON 原样填入 DSL规则列不修改，结果列和备注列由自己分析填入
    - 页面异常 → 不调用子 skill，直接写回，备注列描述 HTTP 状态码和浏览器情况
    - 检测失败 → 不调用子 skill，直接写回，备注列描述失败原因
    - **严禁跳过子 skill 直接写回多维表格或输出结论（页面异常/检测失败除外）**
11. **批量模式下每条 URL 后必须 `browser-use close`**。
12. **子 skill 返回内容必须回填到 DSL规则列**：
    - 普通抓取：提取 `rule` 字符串原样填入
    - playwright抓取：完整 DSL JSON 原样填入不修改
13. **修改时间列每次写回前通过 `python references/get_timestamp.py` 获取实时时间填入**，格式 `yyyy-MM-dd HH:mm:ss`。
14. 🔴 **逐条处理，逐条写回**：读取 → 检测 → 子 skill → 整合 → **立即写回当前这条** → 下一条。**严禁将所有结果攒到最后批量写回。** 写回必须在处理下一条之前完成。
15. **最多 200 条每页**，`has_more` 时串行翻页。
16. **批量模式下，B4 汇总报告仅在所有记录处理并写回完成后输出**，汇总阶段不再写回。

## 清理

- 单个 URL 模式：检测完成后 `browser-use close` + 删除临时目录。
- 批量模式：每条 URL 后 `browser-use close`；所有写入完成后删除批次临时目录。
