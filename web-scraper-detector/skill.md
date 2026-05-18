---
name: web-scraper-detector
description: 网站抓取方式检测器。HTTP GET 请求 → 浏览器渲染 → 对比判定 → 自动调用子 skill 生成抓取配置。HTTP 500/反爬/内容差异大 → playwright 抓取；其余 → 普通抓取。
---

# 网站抓取方式检测器

以**独立子上下文（Agent）**方式运行。调用方通过 Agent 工具传入 URL，本 skill 在独立上下文中执行完整 6 步检测，返回统一 JSON 后上下文即释放。

执行流程：HTTP GET 请求 → 浏览器渲染 → 对比判定 → 调用子 skill 配置抓取规则 → 输出统一 JSON。

---

## 执行流程（6 步，严格按顺序执行）

### 第一步：HTTP GET 请求

```bash
TEMP=$(mktemp -d)
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

**HTTP 返回 500 时，跳过第二步（浏览器渲染）和第三步（对比），直接进入第四步判定，结果固定为 playwright抓取。**

提取指标（禁止使用 `grep -P`）：

```bash
# 内容长度（字节数）
wc -c < "$TEMP/http_response.html"

# 标题
LC_ALL=C grep -oiE '<title[^>]*>[^<]+</title>' "$TEMP/http_response.html" | head -1

# <script> 数量
LC_ALL=C grep -ci '<script' "$TEMP/http_response.html"

# 反爬关键词命中数（无匹配时输出 0）
LC_ALL=C grep -oicE 'captcha|challenge|access.denied|cf.browser|incapsula|验证码|Cloudflare.protection' "$TEMP/http_response.html" || echo 0

# 有效链接数：<a href="http..."> 且文本长度 >= 5
LC_ALL=C grep -oiE '<a [^>]*href="http[^"]*"[^>]*>[^<]*(<[^/][^>]*>[^<]*)*</a>' "$TEMP/http_response.html" \
  | sed 's/<[^>]*>//g' | LC_ALL=C sed 's/^[[:space:]]*//;s/[[:space:]]*$//' \
  | awk 'length >= 5' | wc -l

# 前 3 条链接文本
LC_ALL=C grep -oiE '<a [^>]*href="http[^"]*"[^>]*>[^<]*(<[^/][^>]*>[^<]*)*</a>' "$TEMP/http_response.html" \
  | sed 's/<[^>]*>//g' | LC_ALL=C sed 's/^[[:space:]]*//;s/[[:space:]]*$//' \
  | awk 'length >= 5' | head -3
```

记录以下 7 个指标：

| 指标 | 含义 |
|------|------|
| `http_code` | HTTP 状态码（超时/报错记 0） |
| `content_length_http` | 响应体字节数 |
| `title_http` | `<title>` 标签内容 |
| `script_count_http` | `<script>` 标签出现次数 |
| `anti_bot_http` | >0 表示命中反爬关键词 |
| `meaningful_link_count_http` | 带 http href 且文本 ≥5 字符的 `<a>` 数量 |
| `first_link_texts_http` | 前 3 条链接文本 |

---

### 第二步：浏览器渲染

**前置条件：HTTP 返回 500 时跳过此步，直接进入第四步判定。**

第一步完成至少 3 秒后执行。使用逐条 eval，**禁止** `browser-use get html` / `browser-use state`：

```bash
sleep 3
browser-use open "$URL"
```

```bash
browser-use get title
browser-use eval "document.querySelectorAll('script').length"
browser-use eval "document.documentElement.outerHTML.length"
browser-use eval "document.body.innerText.includes('captcha') || document.body.innerText.includes('challenge') || document.body.innerText.includes('Cloudflare')"
browser-use eval "Array.from(document.querySelectorAll('a[href]')).filter(function(a){var t=a.textContent.trim();return /^https?:/i.test(a.href)&&t.length>=5}).length"
browser-use eval "Array.from(document.querySelectorAll('a[href]')).filter(function(a){var t=a.textContent.trim();return /^https?:/i.test(a.href)&&t.length>=5}).slice(0,3).map(function(a){return a.textContent.trim()}).join(' ||| ')"
```

提取完成后：
```bash
browser-use close
```

---

### 第三步：对比

| 维度 | HTTP 侧 | 浏览器侧 | 可疑信号 |
|------|---------|----------|----------|
| HTTP 状态 | `http_code` | 页面是否正常加载 | 状态类别不一致 |
| 标题 | grep 结果 | `browser-use get title` | 变为 "Just a moment..." / "Attention Required!" / "安全检查" |
| 内容长度 | `wc -c` | `outerHTML.length` | 差异 > 30% |
| 脚本数 | `grep -c <script` | `script` 数量 | 浏览器侧明显更多 |
| 反爬 | grep 命中数 | body 文本检查 | 任一侧 >0 |
| 有效链接（文本≥5） | HTTP grep 计数 | 浏览器 eval 计数 | HTTP=0 但浏览器有链接 |

---

### 第四步：判定

按顺序匹配，命中即停：

| 条件 | 判定结果 |
|------|----------|
| HTTP 返回 500 | **playwright抓取**（跳过第二步） |
| HTTP 和浏览器均 404/410/50x | **页面异常** |
| HTTP 和浏览器均网络层失败 | **检测失败** |
| HTTP 有效链接 = 0，浏览器有效链接 > 0 | **playwright抓取** |
| 标题变为 challenge/captcha 页面 | **playwright抓取** |
| HTTP 403/503 且浏览器正常 | **playwright抓取** |
| 内容长度差异 > 30% 且浏览器脚本数 > HTTP | **playwright抓取** |
| HTTP 超时/连接失败 且浏览器正常 | **playwright抓取** |
| 以上均不命中 | **普通抓取** |

---

### 第五步：调用子 Skill

**规则：只有判定为"普通抓取"或"playwright抓取"时才调用子 skill。"页面异常"和"检测失败"跳过此步。**

| 判定结果 | 调用的子 skill |
|----------|---------------|
| 普通抓取 | `static-scraper-config` |
| playwright抓取 | `dynamic-scraper-config` |
| 页面异常 | 不调用 |
| 检测失败 | 不调用 |

使用 `Skill` 工具，`skill` 参数传子 skill 名称，`args` 传 URL 和检测摘要。**必须实际触发工具调用，不可推测结果。**

**子 skill 调用成功后**，记录：
- `sub_skill.name` = 子 skill 名称
- `sub_skill.called` = `true`
- `sub_skill.output` = 子 skill 返回的完整 JSON
- `sub_skill.error` = `null`

**子 skill 调用失败时**，记录：
- `sub_skill.name` = 子 skill 名称
- `sub_skill.called` = `false`
- `sub_skill.output` = `null`
- `sub_skill.error` = `"<超时/报错/非JSON返回，具体原因>"`

---

### 第六步：输出统一 JSON

子 skill 返回后，用以下格式输出最终结果：

```json
{
  "url": "<原始URL>",
  "detection": {
    "http_code": <int>,
    "title_http": "<title>",
    "content_length_http": <int>,
    "script_count_http": <int>,
    "anti_bot_http": <int>,
    "meaningful_link_count_http": <int>,
    "browser_title": "<title>",
    "browser_script_count": <int>,
    "browser_content_length": <int>,
    "browser_meaningful_link_count": <int>
  },
  "verdict": "<普通抓取|playwright抓取|页面异常|检测失败>",
  "sub_skill": {
    "name": "<static-scraper-config|dynamic-scraper-config|null>",
    "called": true|false,
    "output": <子 skill 返回的完整 JSON，失败时为 null>,
    "error": "<失败原因|null>"
  }
}
```

此 JSON 为**唯一最终输出**。输出此 JSON 后立即结束，不要附加任何说明、总结、建议或其他文字。

---

## 核心规则

1. 第一步必须先于第二步。**禁止并行或颠倒。**
2. 两步之间 `sleep 3`。
3. 第二步使用逐条 eval。**禁止** `browser-use state` / `browser-use get html`。
4. 所有 grep 加 `LC_ALL=C` 前缀。用 `grep -E`，不用 `grep -P`。
5. HTTP 超时可重试 1 次。浏览器超时不重试。
6. HTTP 非 200 但有 body 仍需执行第二步（HTTP 500 除外，直接跳过二/三步进入 playwright抓取判定）。
7. 判定为"普通抓取"或"playwright抓取"时**必须实际调用 Skill 工具**，不可推测子 skill 结果。子 skill 失败时必须在 `sub_skill.error` 中记录原因。
8. 检测完成后 `browser-use close` + `rm -rf "$TEMP"`。
9. 第六步 JSON 输出后立即结束，不附加任何文字。
