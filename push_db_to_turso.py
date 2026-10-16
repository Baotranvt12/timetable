"""Chép toàn bộ data/timetable.db (máy này) lên database Turso.

Cấu hình Turso đọc từ .streamlit/secrets.toml ([turso] url, auth_token) hoặc biến môi
trường TURSO_DATABASE_URL / TURSO_AUTH_TOKEN.

    python push_db_to_turso.py           # dừng nếu Turso đã có dữ liệu TKB
    python push_db_to_turso.py --force   # xoá dữ liệu trên Turso rồi chép lại
"""
import os
import sqlite3
import sys
import tempfile
import tomllib
from pathlib import Path

import libsql

SRC = Path("data/timetable.db")
BATCH = 200


def with_retry(fn, tries: int = 5):
    """Mạng tới Turso đôi khi ngắt kết nối — thử lại có giãn cách."""
    import time
    for k in range(tries):
        try:
            return fn()
        except ValueError as e:
            if k == tries - 1 or "error" not in str(e).lower():
                raise
            print(f"    mạng lỗi ({str(e)[:60]}…), thử lại lần {k + 2}", flush=True)
            time.sleep(2 * (k + 1))


def turso_config() -> tuple[str, str]:
    url = os.environ.get("TURSO_DATABASE_URL", "")
    token = os.environ.get("TURSO_AUTH_TOKEN", "")
    secrets = Path(".streamlit/secrets.toml")
    if not url and secrets.exists():
        cfg = tomllib.loads(secrets.read_text()).get("turso", {})
        url, token = cfg.get("url", ""), cfg.get("auth_token", "")
    if not url or not token:
        sys.exit("Thiếu cấu hình Turso: tạo .streamlit/secrets.toml với mục [turso] url, auth_token.")
    return url, token


def main() -> None:
    force = "--force" in sys.argv
    url, token = turso_config()

    src = sqlite3.connect(SRC)
    src.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    objects = src.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type = 'index', name"
    ).fetchall()
    tables = [name for typ, name, _ in objects if typ == "table"]

    replica = Path(tempfile.mkdtemp()) / "replica.db"
    dst = libsql.connect(str(replica), sync_url=url, auth_token=token)
    dst.sync()

    existing = {r[0] for r in dst.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall()}
    if "timetable" in existing:
        n = dst.execute("SELECT COUNT(*) FROM timetable").fetchone()[0]
        if n and not force:
            sys.exit(f"Turso đã có {n} tiết trong bảng timetable — không ghi đè. "
                     "Chạy lại với --force nếu chắc chắn muốn thay bằng dữ liệu máy này.")

    for name in existing:
        dst.execute(f'DROP TABLE IF EXISTS "{name}"')
    for typ, name, sql in objects:
        dst.execute(sql)
    dst.commit()

    for name in tables:
        cols = [r[1] for r in src.execute(f'PRAGMA table_info("{name}")')]
        col_sql = ", ".join(f'"{c}"' for c in cols)
        one = "(" + ", ".join("?" * len(cols)) + ")"
        rows = src.execute(f'SELECT {col_sql} FROM "{name}"').fetchall()
        size = max(1, min(BATCH, 30000 // len(cols)))   # giới hạn số tham số của SQLite
        for i in range(0, len(rows), size):
            chunk = rows[i:i + size]
            sql = f'INSERT INTO "{name}" ({col_sql}) VALUES ' + ", ".join([one] * len(chunk))
            params = tuple(v for r in chunk for v in r)
            with_retry(lambda: (dst.execute(sql, params), dst.commit()))
        print(f"  {name}: {len(rows)} dòng", flush=True)
    dst.sync()

    bad = []
    for name in tables:
        a = src.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        b = dst.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        if a != b:
            bad.append(f"{name}: máy {a} / Turso {b}")
    print("Đối chiếu: KHỚP toàn bộ" if not bad else "LỆCH: " + "; ".join(bad))


if __name__ == "__main__":
    main()
