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

## 第二步：先找列表容器，再验证容器内的链接

**核心原则：必须先定位列表容器，再在容器内部判断 `<a>` 标签是否符合规则。禁止在容器定位之前直接全局提取 `<a>` 标签。**

### 2.1 验证标准

一个 URL 可被普通抓取的充分条件是：**在源码中找到至少一组列表容器，且容器内的 `<a>` 标签满足以下条件：**

1. **`@href` 指向详情页**：href 值不是 `#`、不是 `javascript:`、不是当前页锚点
2. **`text()` 是标题**：链接文本非空、长度 ≥ 5 个字符、不全是导航词（如"首页""更多""阅读全文"）
3. **重复模式**：同类容器在页面主体区域重复出现 ≥ 3 次，构成列表

### 2.2 验证流程

#### 2.2.1 定位列表容器（必须先执行）

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

**如果找不到任何出现 ≥ 3 次的容器 → 不可普通抓取**，`scrapable: false`。

#### 2.2.2 在容器内提取并验证 `<a>` 标签

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

#### 2.2.3 确认列表项结构

对候选容器的**所有实例**进行抽样验证（至少抽查 3 个），确认：

- 每个容器实例内都能找到至少 1 个满足条件的 `<a>` 标签
- 容器内的 `<a>` 标签结构一致（如都在 `<h2>` 或 `<h3>` 内，或都在同一层级）
- 链接文本具有标题特征（不是"查看更多""阅读详情"等通用操作词）

全部满足 → **此 URL 可被普通抓取**。

---

## 第三步：输出

```json
{
  "scrapable": true,
  "method": "普通抓取",
  "rule": "默认规则"
}
```

- 满足默认规则 → `scrapable: true`，`method: "普通抓取"`，`rule: "默认规则"`
- 不满足 → `scrapable: false`，输出"不满足"
