"""
Overseas 批量检测脚本。
逐条取 language=2 AND finished IS NULL 的记录，
curl HTTP GET → 提取指标 → 判定 → 写入 DB。
无 subagent 开销，约 5-10 秒/条。
Ctrl+C 中断后会释放占用态记录。
"""
import subprocess
import json
import re
import sys
import os
import signal
import tempfile
import shutil
from references.db import query_one_overseas, update

occupied_ids = []  # 记录当前占用的 id，中断时释放


def cleanup_occupied():
    """释放所有占用态记录（finished=-1 → NULL）"""
    if not occupied_ids:
        return
    from references.db import get_conn
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for rid in occupied_ids:
                cur.execute(
                    "UPDATE recent_invalid_website SET finished = NULL WHERE id = %s",
                    (rid,),
                )
            conn.commit()
        print(f"\n释放 {len(occupied_ids)} 条占用记录")
    finally:
        conn.close()


def signal_handler(sig, frame):
    cleanup_occupied()
    print("\n[中断] 已释放占用态，退出。")
    sys.exit(1)


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ── curl headers ──────────────────────────────────────────
CURL_HEADERS = [
    "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "-H", "Accept-Language: en-US,en;q=0.9",
    "-H", "Accept-Encoding: identity",
]


def _decode_html(raw):
    """解码海外网站 HTML：探测 meta charset → UTF-8 → Western 编码回退"""
    detected = None
    head = raw[:2048]
    m = re.search(rb'<meta[^>]*charset[="\s]+([^"\s;>]+)', head, re.IGNORECASE)
    if m:
        detected = m.group(1).decode("ascii", errors="ignore").lower().strip()

    candidates = [enc for enc in ([detected] if detected else []) if enc != "utf-8"]
    candidates += ["utf-8", "windows-1252", "iso-8859-1", "latin-1"]

    for enc in candidates:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue

    return raw.decode("latin-1", errors="replace")


def _try_curl(target_url, html_path):
    """执行单次 curl 请求，返回 http_code 或 0"""
    try:
        result = subprocess.run(
            [
                "curl", "-s", "-L", "-o", html_path, "-w", "%{http_code}",
                "--max-time", "15", "--max-redirs", "3",
                *CURL_HEADERS,
                target_url,
            ],
            capture_output=True, text=True, timeout=20,
        )
        code_str = result.stdout.strip()
        return int(code_str) if code_str.isdigit() else 0
    except (subprocess.TimeoutExpired, Exception):
        return 0


# 软 404 关键词（body 文本中出现这些词 + 链接极少 = soft 404）
SOFT_404_PATTERNS = [
    "not found", "no longer exists", "can't be found", "page not found",
    "doesn't exist", "nothing here", "no results", "couldn't find",
    "the page you were looking for", "this page has been removed",
    "page has moved", "page no longer available", "sorry, we couldn't",
    "sorry, the page", "error 404", "404 error", "page not available",
    "content not found", "the requested page", "we can't find",
    "unable to find", "page doesn't", "has been deleted",
    "has been removed", "no longer available", "page is no longer",
    "this article has been", "this post has been",
]


def _detect_soft_404(body_text, a_count, li_count, title_a_count, title):
    """基于 body 文本关键词 + 结构信号检测软 404"""
    text_lower = body_text.lower()
    match_keywords = [kw for kw in SOFT_404_PATTERNS if kw in text_lower]
    if not match_keywords:
        return False, []
    # 结构信号：链接极少 + 无列表
    structured = (a_count + li_count + title_a_count) >= 5
    if structured:
        return False, []
    return True, match_keywords


def check_url(url):
    """
    对单个 URL 执行 HTTP GET + 指标提取 + 判定。
    - 首次按原 URL 请求，超时可重试 1 次
    - 若 HTTP_CODE=0 且 URL 为 http://，自动升级为 https:// 重试
    返回 dict，keys 与 subagent JSON 一致。
    """
    tmpdir = tempfile.mkdtemp(prefix="batch_")
    html_path = os.path.join(tmpdir, "http_response.html")
    upgraded_to_https = False

    # ── 1. HTTP GET（原 URL，超时可重试 1 次）──
    http_code = 0
    for _ in range(2):
        http_code = _try_curl(url, html_path)
        if http_code != 0:
            break

    # 原 URL 请求失败且为 HTTP 协议 → 升级到 HTTPS 重试
    if http_code == 0 and url.startswith("http://"):
        https_url = "https://" + url[len("http://"):]
        upgraded_to_https = True
        # 清除前次残留
        if os.path.exists(html_path):
            try:
                os.remove(html_path)
            except OSError:
                pass
        for _ in range(2):
            http_code = _try_curl(https_url, html_path)
            if http_code != 0:
                break

    # ── 2. 提取指标 ──
    content_length = 0
    title = ""
    li_count = 0
    ul_ol_count = 0
    a_count = 0
    title_a_count = 0
    body_text_length = 0
    body_text = ""

    if os.path.exists(html_path):
        try:
            content_length = os.path.getsize(html_path)
        except OSError:
            content_length = 0

        if content_length > 0:
            try:
                with open(html_path, "rb") as f:
                    raw = f.read()
            except Exception:
                raw = b""

            html = _decode_html(raw) if raw else ""

            # title
            m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
            if m:
                title = m.group(1).strip()

            # li count
            li_count = len(re.findall(r"<li[ >]", html, re.IGNORECASE))

            # ul/ol count
            ul_ol_count = len(re.findall(r"<ul[ >]|<ol[ >]", html, re.IGNORECASE))

            # a count
            a_count = len(re.findall(r"<a [^>]*href=", html, re.IGNORECASE))

            # title-type a: href + text >= 8 chars
            title_a_matches = re.findall(
                r'<a [^>]*href="[^"]*"[^>]*>([^<]{8,})</a>', html, re.IGNORECASE
            )
            title_a_count = len(title_a_matches)

            # body text
            body_match = re.search(r"<body[^>]*>(.*?)</body>", html, re.DOTALL | re.IGNORECASE)
            if body_match:
                body_text = re.sub(r"<[^>]*>", "", body_match.group(1))
                body_text = re.sub(r"\s+", " ", body_text).strip()
                body_text_length = len(body_text) if body_text else 0

    # ── 3. 判定 ──
    if http_code == 404:
        verdict = "页面不存在(404)"
    elif http_code == 410:
        verdict = "页面不存在(410)"
    elif http_code == 0:
        verdict = "无法访问"
    elif 500 <= http_code < 600:
        verdict = f"服务器错误({http_code})"
    elif http_code == 403:
        verdict = "拒绝访问(403)"
    elif 300 <= http_code < 400:
        verdict = "重定向"
    elif http_code == 200 and body_text_length < 100:
        verdict = "内容过少"
    elif http_code == 200:
        is_soft404, soft404_kws = _detect_soft_404(body_text, a_count, li_count, title_a_count, title)
        if is_soft404:
            verdict = "页面失效(软404)"
        else:
            verdict = "正常访问"
    else:
        verdict = "访问异常"

    # HTTP 200 但零链接 + body 极短 → 真空白页，http_code 覆写为 0
    # body 较长的可能是 SPA 动态加载，不覆写 http_code
    if http_code == 200 and (li_count + a_count + title_a_count) == 0 and body_text_length < 500:
        http_code = 0

    # 软 404 → http_code 覆写为 404
    if verdict == "页面失效(软404)":
        http_code = 404

    accessible = verdict in ("正常访问", "重定向", "内容过少")
    has_list = (verdict == "正常访问" and (li_count + title_a_count) >= 5)

    # ── 4. 清理临时文件 ──
    shutil.rmtree(tmpdir, ignore_errors=True)

    result = {
        "url": url,
        "http_code": http_code,
        "title": title,
        "content_length": content_length,
        "li_count": li_count,
        "ul_ol_count": ul_ol_count,
        "a_count": a_count,
        "title_a_count": title_a_count,
        "body_text_length": body_text_length,
        "verdict": verdict,
        "has_list": has_list,
        "accessible": accessible,
    }
    if upgraded_to_https and http_code != 0:
        result["note"] = "HTTP→HTTPS自动升级"
    if verdict == "页面失效(软404)":
        result["soft404_kws"] = soft404_kws
    return result


def make_memo(result):
    """按结果总结规则生成 memo 字符串"""
    v = result["verdict"]
    note_prefix = "[HTTPS升级] " if result.get("note") else ""
    if "正常访问" in v:
        title = result["title"] or "(无)"
        if len(title) > 50:
            title = title[:50] + "..."
        list_status = "有" if result["has_list"] else "无"
        total = result["li_count"] + result["title_a_count"]
        spa_tag = " [疑似动态加载]" if (result["li_count"] + result["a_count"] + result["title_a_count"]) == 0 and result["body_text_length"] >= 500 else ""
        return f"{note_prefix}正常访问{spa_tag} | 标题={title} | 列表={list_status}({result['li_count']}li+{result['title_a_count']}a) | 大小={result['content_length']}字节 | a={result['a_count']} li={result['li_count']} ta={result['title_a_count']}"
    elif "页面不存在" in v:
        code = result["http_code"]
        return f"{note_prefix}页面不存在({code})"
    elif "页面失效" in v:
        kws = result.get("soft404_kws", [])
        kws_str = ",".join(kws[:3])
        title = result["title"] or "(无)"
        return f"页面失效(软404) | 标题={title} | 命中={{{kws_str}}} | 链接数={result['a_count']}"
    elif "无法访问" in v:
        return "无法访问 | 连接失败/超时"
    elif "服务器错误" in v:
        return f"{note_prefix}服务器错误({result['http_code']})"
    elif "拒绝访问" in v:
        return f"{note_prefix}拒绝访问(403)"
    elif "重定向" in v:
        return f"{note_prefix}重定向({result['http_code']})"
    elif "内容过少" in v:
        title = result["title"] or "(无)"
        return f"{note_prefix}内容过少 | 标题={title} | 大小={result['content_length']}字节 | a={result['a_count']} li={result['li_count']} ta={result['title_a_count']}"
    else:
        return f"{note_prefix}访问异常 | HTTP={result['http_code']}"


def main():
    # 计数器
    count_total = 0
    count_accessible = 0
    count_with_list = 0
    count_404 = 0
    count_soft404 = 0
    count_inaccessible = 0
    count_redirect = 0
    count_error = 0

    print("=" * 60)
    print("Overseas 批量检测 — 逐条 curl + DB 更新")
    print("Ctrl+C 安全中断，自动释放占用态")
    print("=" * 60)

    while True:
        # 取一条未处理记录（自动设 finished=-1）
        row = query_one_overseas()
        if row is None:
            print("\n>>> 没有更多待处理记录，全部完成。")
            break

        rid = row["id"]
        url = row["url"]
        occupied_ids.append(rid)

        if not url or not url.strip():
            memo = "URL为空"
            update(rid, "0", memo, 0)
            occupied_ids.remove(rid)
            count_total += 1
            count_error += 1
            print(f"[{count_total}] id={rid} | SKIP: URL为空")
            continue

        # 检测
        result = check_url(url)
        memo = make_memo(result)
        http_code_str = str(result["http_code"])

        # 写入 DB
        update(rid, http_code_str, memo, result["content_length"])
        occupied_ids.remove(rid)

        # 计数器
        count_total += 1
        v = result["verdict"]
        if "正常访问" in v:
            count_accessible += 1
            if result["has_list"]:
                count_with_list += 1
        elif "内容过少" in v:
            count_accessible += 1
        elif "重定向" in v:
            count_redirect += 1
        elif "页面不存在" in v:
            count_404 += 1
        elif "页面失效" in v:
            count_soft404 += 1
        elif v in ("无法访问", "拒绝访问(403)") or "服务器错误" in v:
            count_inaccessible += 1
        else:
            count_error += 1

        # 进度输出
        upgrade_tag = " [HTTPS↑]" if result.get("note") else ""
        soft404_tag = " [SOFT404]" if result.get("soft404_kws") else ""
        list_tag = "[LIST]" if result["has_list"] else ""
        print(f"[{count_total}] id={rid} | {result['verdict']}{upgrade_tag}{soft404_tag} {list_tag} | {result['title'][:40] if result['title'] else '(无标题)'} | {url[:60]}")

    # ── 汇总 ──
    print()
    print("## 批量检测完成 — 模式: overseas")
    print()
    print("| 判定结果 | 数量 |")
    print("|----------|------|")
    print(f"| 正常访问 | {count_accessible} |")
    print(f"| 其中有列表 | {count_with_list} |")
    print(f"| 404/410 | {count_404} |")
    print(f"| 软404 | {count_soft404} |")
    print(f"| 无法访问 | {count_inaccessible} |")
    print(f"| 重定向 | {count_redirect} |")
    print(f"| 异常/失败 | {count_error} |")
    print()
    print(f"共处理 {count_total} 条。")


if __name__ == "__main__":
    main()
