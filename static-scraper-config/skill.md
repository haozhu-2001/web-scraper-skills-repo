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

## 第二步：在源码中验证列表项

### 2.1 验证标准

一个 URL 可被普通抓取的充分条件是：**在源码中找到至少一组重复出现的 `<a>` 标签，满足：**

1. **`@href` 指向详情页**：href 值不是 `#`、不是 `javascript:`、不是当前页锚点
2. **`text()` 是标题**：链接文本非空、长度 ≥ 5 个字符、不全是导航词（如"首页""更多""阅读全文"）
3. **重复模式**：同类 `<a>` 标签在页面主体区域重复出现 ≥ 3 次，构成列表

### 2.2 验证流程

#### 2.2.1 提取候选链接

从源码中提取所有带 `href` 的 `<a>` 标签，要求文本长度 ≥ 5：

```bash
LC_ALL=C grep -oiE '<a [^>]*href="https?://[^"]*"[^>]*>[^<]+</a>' "$TEMP/source.html" \
  | sed 's/<[^>]*>//g' | LC_ALL=C sed 's/^[[:space:]]*//;s/[[:space:]]*$//' \
  | awk 'length >= 5'
```

#### 2.2.2 过滤非列表项

从候选链接中剔除明显不是列表项的内容：

- 导航栏链接：文本为"首页""Home""上一页""下一页"等
- 侧边栏/页脚链接：需结合容器位置排除
- 链接文本全是标签名/分类名但无标题特征

#### 2.2.3 确认列表容器

- 在源码中找到一组同级、同标签名、同 class 的容器元素
- 每个容器内都能找到满足条件的 `<a>`（有 `@href` + 有标题文本）
- 这组容器在页面主体区域（非 `<nav>`、`<aside>`、`<footer>`）

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
