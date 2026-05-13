---
name: static-scraper-config
description: 配置普通抓取规则，定义目标网站的抓取字段、URL 模式和输出格式。
---

# Static Scraper Config

配置普通抓取（HTTP 直接请求）的抓取规则。

## 何时使用

- 对已判定为"普通抓取"的 URL，配置列表页抓取规则
- 用户需要定义抓取的 URL 模式和输出格式
- 用户需要调整已有抓取规则的字段映射

---

# 默认规则：源码验证列表项

**核心原则：纯 HTTP 请求获取源码，在源码中验证列表项 `<a>` 标签的 `@href` 和 `text()` 是否构成合法条目。不依赖浏览器。**

---

## 第一步：HTTP 请求获取页面源码（强制，不可跳过）

用 curl 发起 GET 请求，携带真实浏览器 User-Agent：

```bash
HTTP_CODE=$(curl -s -o "$TEMP/source.html" -w "%{http_code}" \
  --max-time 15 \
  --max-redirs 5 \
  -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36" \
  -H "Accept: text/html,application/xhtml+xml" \
  -H "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8" \
  "$URL")
echo "HTTP_CODE=$HTTP_CODE"
wc -c < "$TEMP/source.html"
```

- HTTP 返回 200 + 有意义的 HTML 内容 → 继续
- HTTP 超时 / 空响应 / 403 / 503 → **不可普通抓取**，改为 `dynamic-scraper-config` 流程

---

## 第二步：URL 类型预判（强制，不可跳过）

检查目标 URL 是否包含 `tools.prnewswire.com`：

- **是** → 跳过第三步（默认规则验证），直接进入第五步 XPath 规则配置，使用 **Widget 类型配置**（5.3.2）
- **否** → 继续第三步，验证默认规则

---

## 第三步：先找列表容器，再验证容器内的链接

**核心原则：必须先定位列表容器，再在容器内部判断 `<a>` 标签是否符合规则。禁止在容器定位之前直接全局提取 `<a>` 标签。**

### 3.1 验证标准

一个 URL 可被普通抓取的充分条件是：**在源码中找到至少一组列表容器，且容器内的 `<a>` 标签满足以下条件：**

1. **`@href` 指向详情页**：href 值不是 `#`、不是 `javascript:`、不是当前页锚点
2. **`text()` 是标题**：链接文本非空、长度 ≥ 5 个字符、不全是导航词（如"首页""更多""阅读全文"）
3. **重复模式**：同类容器在页面主体区域重复出现 ≥ 3 次，构成列表

### 3.2 验证流程

#### 3.2.1 定位列表容器（必须先执行）

在页面主体区域（排除 `<nav>`、`<aside>`、`<footer>`、`<header>`）中查找重复出现的同级容器元素。容器需满足：

- **同级**：有相同的父元素
- **同标签名**：标签名相同（如都是 `<li>`、`<div>`、`<article>`）
- **同 class**：`class` 属性值相同（或具有相同的结构化 class 模式，如 `item-1`、`item-2`）
- **重复出现**：≥ 3 次

**定位方法**：在源码中搜索常见的列表容器模式：

```bash
# 常见列表容器模式（按优先级排列）：
# 1. <li> 列表项（最可靠）
grep -oP '<li[^>]*class="([^"]*)"[^>]*>' "$TEMP/source.html" | sort | uniq -c | sort -rn | head -10

# 2. <article> 或 <section> 容器
grep -oP '<(article|section)[^>]*class="([^"]*)"[^>]*>' "$TEMP/source.html" | sort | uniq -c | sort -rn | head -10

# 3. <div> 带 class 的容器（最常见但需排除非列表用途）
grep -oP '<div[^>]*class="([^"]*)"[^>]*>' "$TEMP/source.html" | sort | uniq -c | sort -rn | head -20

# 4. <tr> 表格行
grep -oP '<tr[^>]*class="([^"]*)"[^>]*>' "$TEMP/source.html" | sort | uniq -c | sort -rn | head -10
```

筛选出出现次数 ≥ 3 的容器标签/class 组合。对于每种候选容器，在源码中确认：

- 它们的父元素一致
- 它们不在 `<nav>`、`<aside>`、`<footer>`、`<header>` 内部
- 它们的位置连续（相邻兄弟节点之间没有大量无关内容）

**如果找不到任何出现 ≥ 3 次的容器 → 不满足默认规则**，进入第五步判断是否可通过 XPath 规则配置。

#### 3.2.2 在容器内提取并验证 `<a>` 标签

对 2.2.1 中找到的每种候选容器，取其中一个容器作为样本，展开其完整 HTML 内容。在**容器内部**提取所有 `<a>` 标签并验证：

```bash
# 假设已定位到容器，提取容器内满足条件的 <a> 标签：
# 1. href 指向 http/https 的合法 URL
# 2. 链接文本长度 ≥ 5
# 3. href 不是 #、javascript:、mailto:、tel:
grep -oiE '<a [^>]*href="https?://[^"]*"[^>]*>[^<]{5,}</a>'
```

对容器内的 `<a>` 标签进行评分：

| 检查项 | 通过条件 | 权重 |
|--------|---------|------|
| href 有效 | 以 `http://` 或 `https://` 开头 | 必须 |
| 链接文本 | 非空、长度 ≥ 5、非纯导航词 | 必须 |
| 标题特征 | 文本包含中文/英文实义词，非纯日期/数字/标签 | 建议 |
| 唯一性 | 同一容器内不同 `<a>` 的文本不重复 | 建议 |

#### 3.2.3 确认列表项结构

对候选容器的**所有实例**进行抽样验证（至少抽查 3 个），确认：

- 每个容器实例内都能找到至少 1 个满足条件的 `<a>` 标签
- 容器内的 `<a>` 标签结构一致（如都在 `<h2>` 或 `<h3>` 内，或都在同一层级）
- 链接文本具有标题特征（不是"查看更多""阅读详情"等通用操作词）

全部满足 → **此 URL 可被普通抓取**，使用默认规则。

---

## 第四步：默认规则输出

默认规则满足时输出：

```json
{
  "scrapable": true,
  "method": "普通抓取",
  "rule": "默认规则"
}
```

- 满足默认规则 → `scrapable: true`，`method: "普通抓取"`，`rule: "默认规则"`，流程结束
- 不满足 → 进入第五步，尝试 XPath 规则配置

---

# 第五步：XPath 规则配置（默认规则不满足时，或 URL 含 `tools.prnewswire.com` 时）

**适用场景**：
- URL 含 `tools.prnewswire.com`（第二步直接跳入，无需经过默认规则验证）
- 第三步未能定位到 ≥ 3 次重复的列表容器，但页面源码中确实存在可抓取的列表项（如容器 class 不统一、列表项散布在不同父级下、或使用非标准结构）

**核心原则：在源码中逐个定位列表项，提取 XPath，验证后根据 URL 类型选择对应的操作组件规则。**

## 5.1 读取参考文档（强制）

在生成配置前，必须读取 `references/普通操作规则配置文档.md`，理解两种操作组件的参数规则：

| 组件 | 用途 | 关键参数 |
|------|------|----------|
| `normal_list_page_parser_list_page_parser_use_xpath` | 普通 xpath 列表解析 | `list_xpath_exp`(51), `link_xpath_exp`(52), `title_xpath_exp`(53) |
| `normal_list_page_push_detail_page_parser_use_xpath` | 普通 xpath 列表推送正文 | 无参数，`list_page_type: 2` |
| `normal_list_page_parser_with_content_widget` | widget 类型列表解析 | `lable_xpath`(48), `wait_time`(49) |

## 5.2 在源码中定位列表项并提取 XPath

**注意：以下所有操作都在第一步下载的源码文件（`$TEMP/source.html`）中进行。不依赖浏览器。**

### 5.2.1 在源码中定位一个条目

从页面正文区域（肉眼看到的列表）中随便记下一个条目标题的文字内容。在源码文本中搜索这段文字，找到它在 HTML 中的精确行位置。

```bash
grep -n "条目标题关键字" "$TEMP/source.html"
```

### 5.2.2 在源码中回溯找容器

从该位置向上逐行回溯，找到包裹该条目的最外层容器元素。然后检查它的相邻兄弟：

- 兄弟元素标签名一致吗？
- 兄弟元素内部的子节点层级一致吗？
- 每个兄弟容器内都能找到一个带标题文字、带 `href` 的 `<a>` 标签吗？
- 这组容器在源码中的位置，是在页面主体区域而不是 `<nav>`、`<aside>`、`<footer>` 或横向滚动区吗？

全部符合 → 这就是容器锚点。**在源码中数一下这组容器一共重复了多少次**。

### 5.2.3 在源码中写出三个 XPath

三个 XPath 都以容器锚点为基准。XPath 必须以源码中**真实出现过的**标签名、class 名、属性值为依据。

**严禁以下写法：**
- `contains(@href, '...')`
- `not(contains(...))` 堆叠
- `string-length()`、`normalize-space()`
- `[条件1 and 条件2 and 条件3]` 串联

#### list_xpath_exp —— 列表项容器（绝对路径）

```
//容器标签[@属性='值']
```

示例：`//div[@class='elementor-loop-container elementor-grid']/div`

#### link_xpath_exp —— 链接 XPath（相对路径，以 `.//` 开头）

```
.//a[1]/@href
.//h2/a/@href
.//h3[@class='title']/a/@href
```

- 末尾必须带 `/@href`
- 是**相对路径**，以 `.//` 开头，相对于 `list_xpath_exp` 选中的容器
- 取容器内第一个 `<a>` 或取指定层级下的 `<a>` 的 href 属性

#### title_xpath_exp —— 标题 XPath（相对路径，以 `.//` 开头）

```
.//h2/a/text()
.//h3[@class='title']/a/text()
.//a[1]/text()
```

- 末尾必须带 `/text()`
- 是**相对路径**，以 `.//` 开头，相对于 `list_xpath_exp` 选中的容器
- 取容器内标题标签的文本内容

### 5.2.4 在源码中预验证 XPath

用三个 XPath 的思路，在源码文本中手动验证：

- **list_xpath_exp**：源码中这组容器重复的次数，和页面上肉眼看到的条目数大致一致吗？
- **link_xpath_exp**：每个容器内，按 link_xpath_exp 的路径能找到 `<a href="...">` 且指向详情页（不是 `#`、不是 `javascript:`）吗？
- **title_xpath_exp**：每个容器内，按 title_xpath_exp 的路径能找到可读的标题文字吗？文字内容和页面上显示的一致吗？
- **误命中检查**：源码中其他区域有没有同标签名但结构不同的元素会被这些 XPath 误命中？

不通过 → 回到 4.2.2，调整容器锚点。

### 5.2.5 XPath 验证命令

```bash
# 验证 list_xpath_exp：统计命中数
grep -oP '<容器标签[^>]*class="容器class"[^>]*>' "$TEMP/source.html" | wc -l

# 验证每个容器内都有链接
grep -oP '<a [^>]*href="https?://[^"]*"[^>]*>[^<]{5,}</a>' "$TEMP/source.html" | wc -l

# 验证标题文本存在
grep -oP '<h[23][^>]*class="[^"]*title[^"]*"[^>]*>[^<]+</h[23]>' "$TEMP/source.html" | head -10
```

## 5.3 确定 URL 类型并生成配置

### 5.3.1 URL 类型判断

URL 类型已在第二步确定，此处按以下路径处理：

- **第二步已判为 Widget 类型**（URL 含 `tools.prnewswire.com`）→ 使用 `normal_list_page_parser_with_content_widget` 组件，跳至 5.3.2
- **其他**（第三步默认规则不满足，流入此处的非 Widget URL）→ 普通 XPath 类型，使用 `normal_list_page_parser_list_page_parser_use_xpath` + `normal_list_page_push_detail_page_parser_use_xpath` 组件，跳至 5.3.3

### 5.3.2 Widget 类型配置（URL 含 `tools.prnewswire.com`）

使用 `normal_list_page_parser_with_content_widget` 组件。`lable_xpath` 参数值为 `list_xpath_exp`（列表项容器的绝对 XPath）。

**参数映射**：

| 参数 | param_define_id | param_code | 值来源 |
|------|-----------------|------------|--------|
| `lable_xpath` | 48 | `lable_xpath` | 5.2.3 中的 `list_xpath_exp` |
| `wait_time` | 49 | `wait_time` | 固定值 `"1"` |

**输出格式**：

```json
{
  "scrapable": true,
  "method": "普通抓取",
  "rule": "widget规则",
  "config": {
    "spider_source_id": null,
    "op_list": [
      {
        "operate_id": null,
        "parent_id": null,
        "op_name": "普通列表解析含正文widget",
        "op_type": "normal_list_page_parser_with_content_widget",
        "op_sequence": 1,
        "op_param": [
          {
            "operate_id": null,
            "op_type": "normal_list_page_parser_with_content_widget",
            "param_define_id": 48,
            "param_code": "lable_xpath",
            "param_name": "标签xpath",
            "param_value": "<list_xpath_exp>"
          },
          {
            "operate_id": null,
            "op_type": "normal_list_page_parser_with_content_widget",
            "param_define_id": 49,
            "param_code": "wait_time",
            "param_name": "等待时间",
            "param_value": "1"
          }
        ],
        "sub_op_list": null,
        "spider_type": 7,
        "list_page_type": 1
      }
    ]
  }
}
```

### 5.3.3 普通 XPath 类型配置（URL 不含 `tools.prnewswire.com`）

使用两个组件：列表解析 + 推送正文。

**参数映射**：

| 参数 | param_define_id | param_code | 值来源 |
|------|-----------------|------------|--------|
| `list_xpath_exp` | 51 | `list_xpath_exp` | 5.2.3 中的 `list_xpath_exp` |
| `link_xpath_exp` | 52 | `link_xpath_exp` | 5.2.3 中的 `link_xpath_exp` |
| `title_xpath_exp` | 53 | `title_xpath_exp` | 5.2.3 中的 `title_xpath_exp` |

**输出格式**：

```json
{
  "scrapable": true,
  "method": "普通抓取",
  "rule": "xpath规则",
  "config": {
    "spider_source_id": null,
    "op_list": [
      {
        "operate_id": null,
        "parent_id": null,
        "op_name": "普通列表解析列表列表xpath解析",
        "op_type": "normal_list_page_parser_list_page_parser_use_xpath",
        "op_sequence": 1,
        "op_param": [
          {
            "operate_id": null,
            "op_type": "normal_list_page_parser_list_page_parser_use_xpath",
            "param_define_id": 51,
            "param_code": "list_xpath_exp",
            "param_name": "列表xpath规则",
            "param_value": "<list_xpath_exp>"
          },
          {
            "operate_id": null,
            "op_type": "normal_list_page_parser_list_page_parser_use_xpath",
            "param_define_id": 52,
            "param_code": "link_xpath_exp",
            "param_name": "列表xpath规则链接规则",
            "param_value": "<link_xpath_exp>"
          },
          {
            "operate_id": null,
            "op_type": "normal_list_page_parser_list_page_parser_use_xpath",
            "param_define_id": 53,
            "param_code": "title_xpath_exp",
            "param_name": "列表xpath规则标题规则",
            "param_value": "<title_xpath_exp>"
          }
        ],
        "sub_op_list": null,
        "spider_type": 9,
        "list_page_type": 1
      },
      {
        "operate_id": null,
        "parent_id": null,
        "op_name": "普通列表推送正文列表xpath解析",
        "op_type": "normal_list_page_push_detail_page_parser_use_xpath",
        "op_sequence": 1,
        "op_param": [],
        "sub_op_list": null,
        "spider_type": 9,
        "list_page_type": 2
      }
    ]
  }
}
```

## 5.4 XPath 不可提取时的兜底

如果经过 5.2 的完整流程仍然无法写出可靠的三个 XPath（容器无法定位、链接结构不一致、标题文本无法匹配等），则此 URL 不可通过静态抓取配置规则：

```json
{
  "scrapable": false,
  "method": null,
  "rule": null,
  "reason": "XPath不可提取：<具体原因>"
}
```

**输出此 JSON 后任务结束**，改为 `dynamic-scraper-config` 流程继续尝试。
