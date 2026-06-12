---
name: dynamic-scraper-config
description: 配置动态抓取规则，定义目标网站的抓取字段、URL 模式和输出格式。
---

# Dynamic Scraper Config

配置动态抓取（Playwright 浏览器）的抓取规则。

## 何时使用

- 需要为浏览器渲染的网站配置抓取字段
- 需要定义抓取 URL 模式和输出格式
- 需要调整已有抓取规则的字段映射

---

## 浏览器获取源码规则（强制）

动态抓取的页面源码**必须**通过真实浏览器获取 JS 渲染后的 DOM。

### 优先级

| 优先级 | 方式 | 说明 |
|--------|------|------|
| 1 | `browser-use` skill **无头模式**（默认） | `browser-use open <url>` |
| 2 | `browser-use` skill **有头模式** | `browser-use --headed open <url>`（无头被拦截时） |
| 3 | 查找其他可用的浏览器自动化 skill | 如 `playwright-mcp` 等 |

### 严禁

- ❌ `curl` / `wget` 等命令行 HTTP 工具
- ❌ `Invoke-WebRequest` / `WebClient` 等 PowerShell 静态下载
- ❌ `view-source:` 协议
- ❌ 任何不经过 JS 渲染直接获取 HTML 的方式

### 拿到源码后的流程

`browser-use get html` 或 `browser-use eval "document.documentElement.outerHTML"` → 源码就绪，进入 XPath 分析。

---

## Input

调用方（`web-scraper-detector` 第五步）传入：目标 URL 和检测结论摘要。

本 skill 职责：**获取源码 → XPath 分析 → DOM 验证 → 分页判定 → DSL 生成 → 输出 JSON**。

> 需要自行获取页面源码。调用方的浏览器会话在检测完成后已关闭，不可复用。

---

## 流程总览

| 步骤 | 做什么 | 失败处理 | 详细方法 |
|------|--------|---------|---------|
| 1 | 获取页面源码（强制） | — | `references/xpath-rules.md` §第一步 |
| 2 | 源码中定位容器 + 写 XPath | 调整容器重试 | `references/xpath-rules.md` §第二~三步 |
| 3 | 源码中预验证 XPath | 回到步骤 2 | `references/xpath-rules.md` §第四步 |
| 4 | 真实 DOM 验证 XPath | 回到步骤 2 | `references/xpath-rules.md` §第五步 |
| 5 | 分页机制检测 | — | `references/xpath-rules.md` §分页检测 |
| 6 | 判定分页数 | 降级模式 | `references/pagination-logic.md` |
| 7 | 生成 DSL JSON | — | 本文 §第七步 |
| 8 | XPath 不可提取兜底 | — | 本文 §第八步 |

---

## 第一步~第五步：XPath 解析

**核心原则：先源码，后验证。**

> 完整方法论见 `references/xpath-rules.md`。以下为关键决策点。

### list_item 铁律（唯一必须遵守的规则）

**`data.select` 必须选到标题所在的 `<a>` 标签，严禁选外层 `<div>`/`<tr>`/`<li>`。**

- ✅ 选到 `<a>` → `element.click` 只需 `source="row"` + `open_mode="new_tab"`，绕过弹窗
- ❌ 选到容器 → 需要额外 `selector` 参数，且可能触发挂载在容器上的弹窗拦截

> 完整规则 + 正反例 + 禁用写法见 `references/xpath-rules.md` §第三步。

### 分页检测（在 DOM 验证完成后执行）

1. 找"下一页""加载更多""Next""More"等按钮 → 记录按钮 XPath
2. 无按钮 → 滑到底部 1 次 → 有新列表项记 `"scroll"`，无则记 `null`

### 第五步输出（IN → 第六步）

```json
{ "list_item": "XPath|null", "url": "XPath|null", "title": "XPath|null", "next_page": "XPath|scroll|null" }
```

---

## 第六步：分页数判定

IN: `list_item` XPath + `next_page` 信号 + 首页源码（含日期文本）
OUT: `stop` 值（int） + 模板标识（`"3.3"` | `"3.4"`）

> 完整逻辑见 `references/pagination-logic.md`。日期格式见 `references/date-formats.md`。

### 决策速查

```
1. 提取首页每条列表项的日期。（方法: references/date-formats.md）
2. 检查 [T-2, T-1] 窗口覆盖:
   ├─ 首页已覆盖 T-2                   → 状态 A → 不分页, stop=1, 模板 3.3
   ├─ 首页部分覆盖, T-2 在后页           → 状态 B → 翻页查找, 模板 3.4
   ├─ 首页无 [T-2,T-1], 无分页机制/稀疏   → 状态 C-Ⅰ → 不分页, stop=1, 模板 3.3
   └─ 首页无 [T-2,T-1], 有分页且列表塞满  → 状态 C-Ⅱ → 升级为 B, 翻页查找

3. 翻页查找（仅状态 B/C-Ⅱ）:
   三层退出（哪个先触发用哪个）:
   - 窗口覆盖: 最旧条目 ≤ T-2 → stop = 当前页
   - 无新增: cur_count == prev_count（仅 append 模式）→ stop = 上一页
   - 硬上限: page ≥ 10（降级模式 5）→ stop = max_pages
```

---

## 第七步：生成 DSL

IN: 第五步的 XPath + 第六步的 stop/模板
OUT: 最终 JSON（见 §7.4 格式）

### 7.1 读取参考文档（强制）

生成前必须读取 `references/操作组件规则配置文档.md`。本文档的 §3 包含四套完整模板 JSON。

### 7.2 模板选择决策

```
1. 用户只要列表字段（不进入详情页）？
   → 是: 3.1/3.2 | 否（默认）: 继续

2. 分页？
   → 状态 A/C-Ⅰ: 3.3 (不分页列表详情)
   → 状态 B/C-Ⅱ: 3.4 (分页列表详情)

3. (仅 3.4) 分页触发方式？
   → next_page = XPath（页码跳转）   → 3.4.1 (点击"下一页"按钮)
   → next_page = XPath（加载更多）    → 3.4.2 (点击"加载更多")
   → next_page = "scroll"          → 3.4.2 (滑动/点击加载更多)
```

### 7.3 关键参数速查

> 完整参数表见 `references/操作组件规则配置文档.md` §2。

**data.select**：`selector` = 第五步 `list_item` 加 `xpath=` 前缀，`save_as="list_rows"`。分页场景 `mode="cover"`。

**data.extract (列表页)**：`source="row"`。select 已到 `<a>` → url=`"xpath=./@href"`, title=`"xpath=./text()"`。

> ⚠️ **fields 禁止精简**：模板中的 fields 是下游 kafka 消费端的固定契约，直接复制模板的 `data.extract` 块，**只替换各 field 的 selector 值，不增删任何 field**。即使多个 field 的 selector 恰巧相同也不要去重——每个 field 独立提取，XPath 随网站变化，下次可能不同。

**data.extract (详情页)**：`source="page"`。`html_content="xpath=//html"` 必填。

**element.click (详情页)**：`source="row"`, `open_mode="new_tab"`。select 已到 `<a>` 时不需 `selector`。

**element.click (翻页)**：`selector` = 第五步 `next_page` XPath 加 `xpath=` 前缀。

**control.loop**：`start=0`, `step=1`, `stop` = 第六步判定值（不分页时 `stop=1`）。

### 7.4 输出格式

```json
{
  "scrapable": true,
  "method": "playwright抓取",
  "rule": "xpath规则",
  "config": {
    "spider_source_id": null,
    "entry": [ "..." ]
  },
  "reason": null
}
```

输出规则：
1. 每个 `children` 内 `op_sequence` 从 1 重新编号
2. `spider_source_id` 使用用户提供的值，无则留 `null`
3. `browser.open` 的 `url` 为用户提供的列表页 URL，`op_sequence` 为 1
4. 所有 selector 以 `xpath=` 开头
5. `method`=`"playwright抓取"`, `rule`=`"xpath规则"`

---

## 第八步：XPath 不可提取时的兜底

如果经过步骤 2+3 仍无法确定可靠的三 XPath：

```json
{
  "scrapable": false,
  "method": null,
  "rule": null,
  "reason": "XPath不可提取：<具体原因>"
}
```

**输出此 JSON 后任务结束**，不执行步骤 6/7。

---

## 选择器速查

- 只用 XPath；`//` 后代轴；列表页相对路径、详情页绝对路径
- **list_item 选到 `<a>`** → url=`./@href`, title=`./text()`
- 禁用：`contains(@href,...)`, `not(contains(...))` 堆叠, `string-length()`, 多条件 `[and][and]`

> 完整规则见 `references/xpath-rules.md`。
