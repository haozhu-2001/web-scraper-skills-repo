"""
从现有 memo 字段提取字节数，回写到 byte_size 字段。
用法: python backfill_byte_size.py
"""
import re
import sys
from references.db import get_conn

PATTERN = re.compile(r"大小=(\d+)字节")


def extract_byte_size(memo):
    """从 memo 中提取字节数，未找到返回 0"""
    if not memo:
        return 0
    m = PATTERN.search(memo)
    return int(m.group(1)) if m else 0


def main():
    conn = get_conn()
    try:
        # 读取所有记录
        with conn.cursor() as cur:
            cur.execute("SELECT id, memo FROM recent_invalid_website WHERE memo IS NOT NULL AND memo != ''")
            rows = cur.fetchall()

        if not rows:
            print("没有需要处理的记录")
            return

        updated = 0
        skipped = 0
        with conn.cursor() as cur:
            for rid, memo in rows:
                size = extract_byte_size(memo)
                if size > 0:
                    cur.execute(
                        "UPDATE recent_invalid_website SET byte_size = %s WHERE id = %s",
                        (size, rid),
                    )
                    updated += 1
                else:
                    skipped += 1

            conn.commit()

        print(f"处理完成: 更新 {updated} 条, 跳过(无字节数) {skipped} 条, 共 {len(rows)} 条")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
