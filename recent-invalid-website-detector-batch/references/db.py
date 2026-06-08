import pymysql

DB_CONFIG = {
    "host": "192.168.190.253",
    "port": 3306,
    "user": "claude_ai",
    "password": "Claude@123",
    "database": "ai_check",
    "charset": "utf8",
}


def get_conn():
    return pymysql.connect(**DB_CONFIG)


def query_all():
    """查询 recent_invalid_website 表全部记录，返回 list[dict]"""
    conn = get_conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT * FROM recent_invalid_website")
            return cur.fetchall()
    finally:
        conn.close()


def query_by_id(record_id):
    """按 id 查询单条记录"""
    conn = get_conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT * FROM recent_invalid_website WHERE id = %s", (record_id,)
            )
            return cur.fetchone()
    finally:
        conn.close()


def query_one_mainland():
    """取一条 language=2 且未处理(finished IS NULL)的记录，立即设 finished=-1 占用，返回 dict 或 None"""
    conn = get_conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT * FROM recent_invalid_website"
                " WHERE language = 2 AND finished IS NULL"
                " LIMIT 1"
            )
            row = cur.fetchone()
            if row:
                cur.execute(
                    "UPDATE recent_invalid_website SET finished = -1 WHERE id = %s",
                    (row["id"],),
                )
                conn.commit()
            return row
    finally:
        conn.close()


def query_one_overseas():
    """取一条 language!=2 且未处理(finished IS NULL)的记录，立即设 finished=-1 占用，返回 dict 或 None"""
    conn = get_conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT * FROM recent_invalid_website"
                " WHERE language != 2 AND finished IS NULL"
                " LIMIT 1"
            )
            row = cur.fetchone()
            if row:
                cur.execute(
                    "UPDATE recent_invalid_website SET finished = -1 WHERE id = %s",
                    (row["id"],),
                )
                conn.commit()
            return row
    finally:
        conn.close()


def update(record_id, http_code, memo, byte_size=0):
    """更新指定记录的 http_code、memo、byte_size 字段，同时将 finished 标记为 1"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE recent_invalid_website"
                " SET http_code = %s, memo = %s, byte_size = %s, finished = 1"
                " WHERE id = %s",
                (http_code, memo, byte_size, record_id),
            )
        conn.commit()
    finally:
        conn.close()
