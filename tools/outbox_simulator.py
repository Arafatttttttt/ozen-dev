"""Outbox simulator: a line-by-line Python port of the 1C sender logic.

Lets you run Test 2 (resilience) without a 1C infobase: the SQLite table plays the role
of the information register ozs_ОчередьОбменаСБухгалтерией, `run` plays the scheduled job.
Standard library only.

    python tools/outbox_simulator.py enqueue --count 10 --invalid 1
    python tools/outbox_simulator.py run --once
    python tools/outbox_simulator.py run --until-empty --interval 1
    python tools/outbox_simulator.py stats
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

# Same knobs as ozs_ОбменСБухгалтериейНастройки, delays shortened for a live demo.
URL = "http://localhost:8000/api/v1/accounting/sync"
BATCH_SIZE = 50
MAX_ATTEMPTS = 10
BASE_DELAY_SEC = 1
MAX_DELAY_SEC = 8
TIMEOUT_SEC = 5
NON_RETRYABLE = {400, 422}

PENDING, SENT, ERROR = "Ожидает", "Отправлено", "Ошибка"

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id TEXT PRIMARY KEY,               -- Идентификатор (Idempotency-Key)
    object_type TEXT NOT NULL,         -- ТипОбъекта
    body TEXT NOT NULL,                -- ТелоJSON
    status TEXT NOT NULL,              -- Статус
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    next_attempt_at TEXT NOT NULL,
    last_code INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_outbox_queue ON outbox(status, next_attempt_at);
"""


def connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


# --- "Подписка ПриЗаписи": build the snapshot and enqueue it in one transaction ----------

def fake_document(invalid: bool) -> dict:
    price = random.choice([15000, 25000, 40000])
    items = [
        {"line": 1, "name": "Абонемент «Безлимит»", "quantity": 1, "price": price, "amount": price},
        {"line": 2, "name": "Аренда шкафчика", "quantity": 1, "price": 2000, "amount": 2000},
    ]
    doc = {
        "schema_version": "1.0",
        "message_id": str(uuid.uuid4()),
        "source": "1c-fitness",
        "operation": "upsert",
        "version": time.time_ns() // 1_000_000,
        "object_type": "ПродажаАбонемента",
        "document_id": str(uuid.uuid4()),
        "document_number": f"ФК-{random.randint(1, 999999):06d}",
        "document_date": now().isoformat(),
        "client_name": random.choice(["Иванова Анна", "Ким Дмитрий", "Сериков Ерлан"]),
        "amount": price + 2000,
        "currency": "KZT",
        "items": items,
    }
    if invalid:
        doc["items"] = []  # poison message: the receiver must answer 400
    return doc


def enqueue(db: sqlite3.Connection, count: int, invalid: int) -> None:
    with db:  # one transaction, like the document write in 1C
        for i in range(count):
            doc = fake_document(invalid=i < invalid)
            ts = now().isoformat(sep=" ")
            db.execute(
                "INSERT INTO outbox (id, object_type, body, status, created_at, next_attempt_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (doc["message_id"], doc["object_type"], json.dumps(doc, ensure_ascii=False), PENDING, ts, ts),
            )
    print(f"enqueued {count} message(s), {invalid} of them invalid")


# --- "ozs_ОтправкаВБухгалтерию": batch, POST, classify, backoff -----------------------

@dataclass
class SendResult:
    success: bool = False
    retry: bool = True
    network_error: bool = False
    code: int = 0
    error: str = ""


def send(row: sqlite3.Row) -> SendResult:
    request = urllib.request.Request(
        URL,
        data=row["body"].encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Idempotency-Key": row["id"],
            "X-Ozen-Attempt": str(row["attempts"] + 1),
        },
    )
    try:  # Попытка
        with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:
            return SendResult(success=True, retry=False, code=response.status)
    except urllib.error.HTTPError as exc:  # got an answer, but not 2xx
        body = exc.read().decode("utf-8", errors="replace")[:2000]
        return SendResult(retry=exc.code not in NON_RETRYABLE, code=exc.code, error=f"HTTP {exc.code}: {body}")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:  # Исключение: no answer
        reason = getattr(exc, "reason", exc)
        return SendResult(network_error=True, error=f"Сервис недоступен: {reason}")


def retry_delay(attempt: int) -> int:
    delay = min(BASE_DELAY_SEC * 2 ** min(attempt - 1, 20), MAX_DELAY_SEC)
    return int(delay) + random.randint(0, max(int(delay) // 5, 1))


def record_result(db: sqlite3.Connection, row: sqlite3.Row, result: SendResult) -> str:
    attempts = row["attempts"] + 1
    next_at = row["next_attempt_at"]
    if result.success:
        status, error = SENT, ""
    elif not result.retry or (not result.network_error and attempts >= MAX_ATTEMPTS):
        status, error = ERROR, result.error
    else:
        status, error = PENDING, result.error
        next_at = (now() + timedelta(seconds=retry_delay(attempts))).isoformat(sep=" ")
    with db:
        db.execute(
            "UPDATE outbox SET status=?, attempts=?, error=?, last_code=?, next_attempt_at=? WHERE id=?",
            (status, attempts, error, result.code, next_at, row["id"]),
        )
    return status


def process_batch(db: sqlite3.Connection) -> dict:
    rows = db.execute(
        "SELECT * FROM outbox WHERE status=? AND next_attempt_at<=? ORDER BY created_at, rowid LIMIT ?",
        (PENDING, now().isoformat(sep=" "), BATCH_SIZE),
    ).fetchall()
    totals = {"selected": len(rows), SENT: 0, PENDING: 0, ERROR: 0, "service_down": False}
    for row in rows:
        result = send(row)
        totals[record_result(db, row, result)] += 1
        if result.network_error:  # circuit breaker: do not wait for a timeout 50 times
            totals["service_down"] = True
            break
    return totals


def stats(db: sqlite3.Connection) -> dict:
    counts = {PENDING: 0, SENT: 0, ERROR: 0}
    for row in db.execute("SELECT status, COUNT(*) AS n FROM outbox GROUP BY status"):
        counts[row["status"]] = row["n"]
    counts["max_attempts"] = db.execute("SELECT COALESCE(MAX(attempts), 0) FROM outbox").fetchone()[0]
    return counts


def print_table(db: sqlite3.Connection) -> None:
    print(f"{'id':<10} {'status':<11} {'att':>3} {'code':>4}  error")
    for row in db.execute("SELECT * FROM outbox ORDER BY created_at, id"):
        print(f"{row['id'][:8]:<10} {row['status']:<11} {row['attempts']:>3} {row['last_code']:>4}  {row['error'][:70]}")


def main() -> int:
    global URL
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=Path("outbox.db"))
    parser.add_argument("--url", default=URL)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_enq = sub.add_parser("enqueue")
    p_enq.add_argument("--count", type=int, default=10)
    p_enq.add_argument("--invalid", type=int, default=0)
    p_run = sub.add_parser("run")
    mode = p_run.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--until-empty", action="store_true", help="loop until nothing is pending")
    p_run.add_argument("--interval", type=float, default=1.0)
    p_run.add_argument("--max-runs", type=int, default=200)
    sub.add_parser("stats")
    sub.add_parser("table")
    sub.add_parser("reset")
    args = parser.parse_args()
    URL = args.url

    db = connect(args.db)
    if args.cmd == "enqueue":
        enqueue(db, args.count, args.invalid)
    elif args.cmd == "run":
        runs = 0
        while True:
            runs += 1
            totals = process_batch(db)
            if totals["selected"]:
                print(f"run {runs:>3}: {totals}")
            if args.once or stats(db)[PENDING] == 0 or runs >= args.max_runs:
                break
            time.sleep(args.interval)
        print("queue:", stats(db))
    elif args.cmd == "stats":
        print(json.dumps(stats(db), ensure_ascii=False))
    elif args.cmd == "table":
        print_table(db)
    elif args.cmd == "reset":
        with db:
            db.execute("DELETE FROM outbox")
        print("queue cleared")
    return 0


if __name__ == "__main__":
    sys.exit(main())
