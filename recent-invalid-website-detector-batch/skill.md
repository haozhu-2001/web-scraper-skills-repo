---
name: recent-invalid-website-detector-batch
description: 快速检测历史失效网站是否恢复。纯 HTTP GET，检查页面 404/可访问/列表项。数据源为 MySQL，单条 URL / mainland 批量 / overseas 批量三种入口。
---

# 历史失效网站批量检测器

对 `ai_check.recent_invalid_website` 表中已失效的 URL 进行复检。纯 HTTP GET，无浏览器渲染，速度快。

数据源：MySQL → `references/db.py`（`query_one_mainland` / `query_one_overseas` / `update`）

**占用机制**：`query_one_*` 取到记录后立即将 `finished` 设为 `-1`（占用态），防止并发重复处理。`update()` 最终将 `finished` 设为 `1`（完成态）。

---

## 入口路由（最先执行，三者互斥）

| 用户输入 | 执行路径 |
|----------|----------|
| 单个 URL（`http://` 或 `https://` 开头） | 步骤 A：启动 subagent 检测 → 输出 JSON → 结束 |
| `mainland` | 步骤 B：循环取 `language=2 AND finished IS NULL` → subagent 检测 → 更新 DB |
| `overseas` | 步骤 B：循环取 `language!=2 AND finished IS NULL` → subagent 检测 → 更新 DB |

---

## 步骤 A：单 URL 检测

启动一个 subagent 执行 HTTP 检测，返回 JSON 后原样输出给用户。

```
Agent(
  subagent_type="general-purpose",
  description="Check if URL is accessible",
  prompt="对以下 URL 执行 HTTP 可达性检测，严格按步骤执行后输出统一 JSON，不附加任何文字。

URL: <用户提供的URL>

### 1. HTTP GET
```bash
TEMP=$(mktemp -d)
HTTP_CODE=$(curl -s -o \"$TEMP/http_response.html\" -w \"%{http_code}\" \
  --max-time 15 --max-redirs 3 \
  -H \"User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36\" \
  -H \"Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8\" \
  -H \"Accept-Language: zh-CN,zh;q=0.9,en;q=0.8\" \
  -H \"Accept-Encoding: identity\" \
  \"<URL>\")
echo \"HTTP_CODE=$HTTP_CODE\"
```
超时/连接失败时 HTTP_CODE=0。超时可重试 1 次。

### 2. 提取指标
```bash
wc -c < \"$TEMP/http_response.html\"
LC_ALL=C grep -oiE '<title[^>]*>[^<]+</title>' \"$TEMP/http_response.html\" | head -1
LC_ALL=C grep -ci '<li[ >]' \"$TEMP/http_response.html\" || echo 0
LC_ALL=C grep -ciE '<ul[ >]|<ol[ >]' \"$TEMP/http_response.html\" || echo 0
LC_ALL=C grep -ci '<a [^>]*href=' \"$TEMP/http_response.html\" || echo 0
# 标题型a标签：有href且文本≥8字的链接，视为列表项
LC_ALL=C grep -oiE '<a [^>]*href=\"[^\"]*\"[^>]*>[^<]{8,}</a>' \"$TEMP/http_response.html\" | wc -l || echo 0
LC_ALL=C sed -n '/<body[^>]*>/,/<\\/body>/p' \"$TEMP/http_response.html\" 2>/dev/null | sed 's/<[^>]*>//g' | sed 's/\\s\\+/ /g' | sed 's/^ *//;s/ *$//' | awk '{len += length} END {print len+0}'
```

### 3. 判定（按顺序，命中即停）
| 条件 | verdict |
|------|---------|
| HTTP_CODE=404 | 页面不存在(404) |
| HTTP_CODE=410 | 页面不存在(410) |
| HTTP_CODE=0 | 无法访问 |
| HTTP_CODE=5xx | 服务器错误(5xx) |
| HTTP_CODE=403 | 拒绝访问(403) |
| HTTP_CODE=30x | 重定向 |
| HTTP_CODE=200 且 body_text_length<100 | 内容过少 |
| HTTP_CODE=200 | 正常访问 |
| 其他 | 访问异常 |

accessible = true 当 verdict 为 正常访问/重定向/内容过少，否则 false。
has_list = true 当 verdict=正常访问 且 (li_count + title_a_count)>=5，否则 false。

### 4. 输出 JSON（唯一输出，不附加任何文字）
{
  \"url\": \"<URL>\",
  \"http_code\": <int>,
  \"title\": \"<title>\",
  \"content_length\": <int>,
  \"li_count\": <int>,
  \"ul_ol_count\": <int>,
  \"a_count\": <int>,
  \"title_a_count\": <int>,
  \"body_text_length\": <int>,
  \"verdict\": \"<verdict>\",
  \"has_list\": true|false,
  \"accessible\": true|false
}

### 5. 清理
rm -rf \"$TEMP\"
"
)
```

---

## 步骤 B：批量检测

### 初始化计数器

```
count_total = 0
count_accessible = 0
count_with_list = 0
count_404 = 0
count_inaccessible = 0
count_redirect = 0
count_error = 0
```

### 循环处理

```
LOOP:
  # 从数据库取一条未处理记录（自动设 finished=-1 占用）
  python -c "from references.db import query_one_mainland; import json; r=query_one_mainland(); print(json.dumps(r,ensure_ascii=False,default=str) if r else '')"
  # 或 query_one_overseas（取决于入口参数）

  IF 返回空 → 没有更多待处理记录，BREAK 到汇总步骤

  解析 row = {id, url, spider_source_id, language, ...}（finished 已自动设为 -1）
  IF row.url 为空 → SKIP，更新 memo='URL为空'，db.update(id, 0, 'URL为空', 0)（update 会将 finished 设为 1），CONTINUE

  # 启动 subagent 检测
  Agent(
    subagent_type="general-purpose",
    description="Check URL: <row.url>",
    prompt="严格按以下步骤执行，完成后输出统一 JSON，不附加任何文字。

对以下 URL 执行 HTTP 可达性检测：

URL: <row.url>

### 1. HTTP GET
```bash
TEMP=$(mktemp -d)
HTTP_CODE=$(curl -s -o \"$TEMP/http_response.html\" -w \"%{http_code}\" \
  --max-time 15 --max-redirs 3 \
  -H \"User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36\" \
  -H \"Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8\" \
  -H \"Accept-Language: zh-CN,zh;q=0.9,en;q=0.8\" \
  -H \"Accept-Encoding: identity\" \
  \"<URL>\")
echo \"HTTP_CODE=$HTTP_CODE\"
```
超时/连接失败时 HTTP_CODE=0。超时可重试 1 次。

### 2. 提取指标
```bash
wc -c < \"$TEMP/http_response.html\"
LC_ALL=C grep -oiE '<title[^>]*>[^<]+</title>' \"$TEMP/http_response.html\" | head -1
LC_ALL=C grep -ci '<li[ >]' \"$TEMP/http_response.html\" || echo 0
LC_ALL=C grep -ciE '<ul[ >]|<ol[ >]' \"$TEMP/http_response.html\" || echo 0
LC_ALL=C grep -ci '<a [^>]*href=' \"$TEMP/http_response.html\" || echo 0
# 标题型a标签：有href且文本≥8字的链接，视为列表项
LC_ALL=C grep -oiE '<a [^>]*href=\"[^\"]*\"[^>]*>[^<]{8,}</a>' \"$TEMP/http_response.html\" | wc -l || echo 0
LC_ALL=C sed -n '/<body[^>]*>/,/<\\/body>/p' \"$TEMP/http_response.html\" 2>/dev/null | sed 's/<[^>]*>//g' | sed 's/\\s\\+/ /g' | sed 's/^ *//;s/ *$//' | awk '{len += length} END {print len+0}'
```

### 3. 判定（按顺序，命中即停）
| HTTP_CODE | verdict |
|-----------|---------|
| 404 | 页面不存在(404) |
| 410 | 页面不存在(410) |
| 0 | 无法访问 |
| 5xx | 服务器错误(5xx) |
| 403 | 拒绝访问(403) |
| 30x | 重定向 |
| 200 + body_text_length<100 | 内容过少 |
| 200 | 正常访问 |
| 其他 | 访问异常 |

accessible = (verdict in [正常访问, 重定向, 内容过少])
has_list = (verdict==正常访问 and (li_count + title_a_count)>=5)

### 4. 输出 JSON（唯一输出）
{
  \"url\": \"<URL>\",
  \"http_code\": <int>,
  \"title\": \"<title>\",
  \"content_length\": <int>,
  \"li_count\": <int>,
  \"ul_ol_count\": <int>,
  \"a_count\": <int>,
  \"title_a_count\": <int>,
  \"body_text_length\": <int>,
  \"verdict\": \"<verdict>\",
  \"has_list\": true|false,
  \"accessible\": true|false
}

### 5. 清理
rm -rf \"$TEMP\"
"
  )

  # 解析 subagent 返回的 JSON
  尝试直接 JSON.parse(subagent_output)
  如果被 markdown 包裹 → 提取 ```json ... ``` 块
  解析失败 → memo='subagent返回非JSON'，db.update(id, 0, memo, 0)，count_error+=1，CONTINUE

  # 结果总结（按下方总结规则生成 memo 字符串）
  memo = 总结(subagent_result)

  # 更新数据库
  python -c "
from references.db import update
update(<id>, '<http_code>', '''<memo>''', <byte_size>)
"

  # 更新计数器
  count_total += 1
  按 verdict 分类累加（规则见下方）

  # subagent 上下文已自动释放，进入下一条
```

### 结果总结规则

主方法从 subagent 返回的 JSON 中提取关键字段，生成一行中文摘要写入 `memo`。

**正常访问**：
```
memo = "正常访问 | 标题={title} | 列表={有/无}({li_count}li+{title_a_count}a) | 大小={content_length}字节"
```
- title 超过 50 字截断加 `...`
- has_list=true → "有"，false → "无"
- 列表项总数 = li_count + title_a_count

**页面不存在**：
```
memo = "页面不存在({http_code})"
```

**无法访问**：
```
memo = "无法访问 | 连接失败/超时"
```

**服务器错误**：
```
memo = "服务器错误({http_code})"
```

**拒绝访问**：
```
memo = "拒绝访问(403)"
```

**重定向**：
```
memo = "重定向({http_code})"
```

**内容过少**：
```
memo = "内容过少 | 标题={title} | 大小={content_length}字节"
```

**访问异常**：
```
memo = "访问异常 | HTTP={http_code}"
```

**subagent 失败**：
```
memo = "检测失败 | subagent返回非JSON"
```

> 注意：`memo` 为纯文本摘要，非 JSON。`http_code` 列仍存原始状态码数字。

### 计数器更新规则

| verdict | 计数器 |
|---------|--------|
| 正常访问 + has_list=true | count_accessible+1, count_with_list+1 |
| 正常访问 + has_list=false | count_accessible+1 |
| 内容过少 | count_accessible+1 |
| 重定向 | count_redirect+1 |
| 页面不存在(404/410) | count_404+1 |
| 无法访问/拒绝访问/服务器错误 | count_inaccessible+1 |
| 访问异常/subagent失败 | count_error+1 |

### 汇总输出

```
## 批量检测完成 — 模式: <mainland/overseas>

| 判定结果 | 数量 |
|----------|------|
| 正常访问 | {count_accessible} |
| 其中有列表 | {count_with_list} |
| 404/410 | {count_404} |
| 无法访问 | {count_inaccessible} |
| 重定向 | {count_redirect} |
| 异常/失败 | {count_error} |

共处理 {count_total} 条。
```

---

## 核心规则

1. 批量模式每轮循环：取 DB（自动设 finished=-1 占用）→ subagent 检测 → 解析 JSON → db.update()（设 finished=1 完成）→ 更新计数器。循环直到 `query_one_*` 返回 None。
2. subagent 上下文独立，返回后自动释放。禁止在主 context 中累积 subagent 返回数据。
3. HTTP 超时可重试 1 次。两次均超时 verdict = "无法访问"。
4. `memo` 列存中文摘要字符串，按"结果总结规则"生成。`http_code` 列存原始状态码数字。
5. 所有 grep 加 `LC_ALL=C`，用 `grep -E` 不用 `grep -P`。
6. subagent 返回非 JSON → 视为检测失败，http_code 填 0，memo 填错误原因。
7. curl 完全无法执行（如未安装）→ 中止批量运行，输出错误。
8. 输出汇总后立即结束，不附加其他文字。
