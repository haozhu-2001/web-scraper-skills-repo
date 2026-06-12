# XPath 规则与方法论

从页面源码中定位列表容器并写出可靠的 XPath。

> 由 `skill.md` 第一~五步引用。本文档定义 XPath 的写法规则和验证方法，skill.md 只描述流程顺序。

---

## 核心原则：先源码，后验证

**源码分析完成之前，不允许在真实页面上执行任何 XPath。**

为什么用源码而不是 DOM：
- 服务端返回的静态 HTML，结构完整、层次清晰，没有 JS 动态插入干扰
- 在源码文本中搜索、定位、计数比动态 DOM 准确
- DOM 仅用于最后验证阶段（见 §4）

---

## 第一步：获取页面源码（强制）

> ⚠️ 动态抓取必须用浏览器获取 JS 渲染后的 DOM。**禁止** curl/Invoke-WebRequest/view-source 等静态方式。

按以下优先级依次尝试，成功即停：

```
1. browser-use skill 无头模式（默认）
   → browser-use open <url>
   
2. browser-use skill 有头模式（无头被 Akamai/反爬拦截时）
   → browser-use close  # 先关掉旧 session
   → browser-use --headed open <url>
   
3. 查找其他可用的浏览器自动化 skill
   → 如 playwright-mcp 等
```

**严禁使用 Bash/PowerShell 的 curl、wget、Invoke-WebRequest 等命令行工具直接下载源码。**

打开页面后：
- `browser-use get html` 获取完整渲染后的 HTML，或 `browser-use eval "document.documentElement.outerHTML"` 取全文
- 列表内容未完全加载时：`browser-use scroll down` 滚动到底部后再取 HTML

---

## 第二步：在源码中定位容器

### 2.1 定位一个条目

在页面正文区域记下一个条目标题文字 → 在源码文本中搜索该文字 → 找到 HTML 精确位置。

### 2.2 回溯找容器

从该位置向上逐行回溯，找包裹该条目的最外层容器。验证：

1. 兄弟元素标签名一致
2. 兄弟元素子节点层级一致
3. 每个兄弟容器内都有一个带标题文字和 `href` 的 `<a>` 标签
4. 容器组在主体区域，不在 `<nav>`/`<aside>`/`<footer>` 或横向滚动区

全部符合 → 容器锚点。**数一下这组容器重复了多少次**，记住这个数字。

---

## 第三步：写出三个 XPath

三个 XPath 以容器锚点为基准，用 `//`（后代轴）连接。

---

### list_item — 列表项容器

> **铁律：`list_item` 应选到标题所在的 `<a>` 标签本身，而非外层 `<div>`/`<tr>`/`<li>`。**
>
> 原因：`data.select` 选到 `<a>` 标签时，`element.click` 只需 `source="row"` + `open_mode="new_tab"` 即可跳转，无需额外 `selector`。这能绕过弹窗拦截（弹窗挂在 `<div>`/`<tr>` 上而非 `<a>` 上）。

| 写法 | 判断 |
|------|------|
| `//tbody[@class='scantbody']/tr` | ❌ 选到 `<tr>`，click 需额外 `selector="xpath=.//td[1]/a"` |
| `//div[contains(@class,'post')]` | ❌ 选到 `<div>`，click 需额外 selector |
| `//tbody[@class='scantbody']/tr/td[1]/a` | ✅ 直接选到 `<a>`，click 只需 `source="row"` |
| `//a[@class='news_item']` | ✅ |
| `//h2/a` | ✅ |

---

### url — 详情页链接（末尾带 `/@href`）

> **铁律：url 必须取标题所在的 `<a>` 标签，严禁取图片/视频/图标等可选元素的 `<a>`。**
>
> 原因：图片可能懒加载、缺省图或无图，导致命中数不一致或取到错误链接。标题 `<a>` 最稳定。

**优先写法**（标题在 `<a>` 内）：

```
.//a[.//h3[@class='title']]/@href
.//a[./h2]/@href
```

**备选写法**（标题标签包裹 `<a>`）：

```
//container[@attr='val']//标题标签/a/@href
```

**禁用写法**：

```
❌ .//a[1]/@href                     ← 第一个 <a> 往往是图片
❌ .//div[@class='thumb']/a/@href    ← 图片容器，部分条目无图片
```

**验证方法**：url XPath 去掉末尾 `/@href`，和 title XPath 去掉末尾 `/text()`，看是否定位到同一个 `<a>` 标签。不是 → url 取到了非标题元素。

---

### title — 条目标题文本

当标题在 `<a>` 标签内时（title 和 url 定位同一个 `<a>`）：

```
//container[@attr='val']//a[@class='link']/text()
```

当标题在独立 `<h2>` 等标签内时：

```
//container[@attr='val']//标题标签/text()
```

---

## 第四步：在源码中预验证

在源码文本中验证（不动 DOM）：

1. **list_item 命中数**：源码中容器重复次数 ≈ 页面上可见条目数？
2. **url 有效性**：每个容器内能找到 `<a href="...">` 且指向详情页（非 `#`、非 `javascript:`）？
3. **url 和 title 同源**：两者定位同一 `<a>`？
4. **title 可读**：文字和页面显示一致？
5. **误命中检查**：其他区域有无同标签名但结构不同的元素会被误命中？

不通过 → 回到第二步调整容器锚点。

---

## 第五步：DOM 验证

**到了这一步，才第一次在真实页面上执行 XPath。** 打开浏览器加载页面后，逐条 eval 执行三个 XPath，检查：

1. **命中数一致**：三个 XPath 命中数相同，且与主列表可见条目数匹配
2. **归属正确**：抽查 2~3 组，url 和 title 归属于对应的 list_item
3. **值正确**：url 指向详情页；title 与页面显示一致
4. **区域纯净**：全部在主体内容区，无误入侧边栏/导航/页脚

不通过 → 回到第二步。

---

## 选择器规则（速查卡）

### 必须遵守

- 只用 XPath
- 标签名、class、属性值必须来自源码中真实出现过的内容
- 容器到内部元素用 `//`（后代轴）
- 列表页提取用相对路径（`./` 或 `.//` 开头），详情页用绝对路径（`//` 开头）
- `url` 末尾带 `/@href`

### 核心原则

- **`data.select` 选到 `<a>` 标签**（避免 `<div>`/`<tr>`/`<li>` 容器）
- **`element.click` 不用 `selector`**（`row` 是 `<a>` 时直接取 `@href` 跳转，绕过弹窗）
- **`data.extract` 跟随简化**（`select` 到 `<a>` → `url="xpath=./@href"`, `title="xpath=./text()"`）

### 严禁写法

- `contains(@href, '...')`
- `not(contains(...))` 堆叠
- `string-length()`、`normalize-space()`
- `[条件1 and 条件2 and 条件3]` 串联

---

## 分页检测

1. 找"下一页""加载更多""Next""More"等按钮 → 有则记录按钮 XPath
2. 无按钮 → 滑到底部 1 次，等约 1 秒
3. 新列表项出现 → `"scroll"`，否则 `null`

只滑 1 次。

---

## 中间输出格式（给 skill.md 第六步消费）

```json
{
  "list_item": "XPath|null",
  "url": "XPath|null",
  "title": "XPath|null",
  "next_page": "XPath|scroll|null"
}
```
