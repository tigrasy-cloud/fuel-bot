"""SQLite-хранилище подписок пользователей."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "bot.db"

MAX_SUBS_PER_CHAT = 5


@dataclass
class Subscription:
    id: int
    chat_id: int
    mode: str          # "city" | "geo"
    lat: float
    lon: float
    place: str
    fuel: str          # "any" | "92" | "95" | ...
    green_ids: list[str]
    created_at: float
    last_checked: float


class Storage:
    def __init__(self, path: Path = DB_PATH) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                place TEXT NOT NULL,
                fuel TEXT NOT NULL DEFAULT 'any',
                green_ids TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL,
                last_checked REAL NOT NULL DEFAULT 0
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                station_id TEXT NOT NULL,
                place TEXT NOT NULL,
                status TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                ts REAL NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reports_station ON reports (station_id, ts)"
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS price_subs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                place TEXT NOT NULL,
                fuel TEXT NOT NULL,
                baseline REAL NOT NULL,
                created_at REAL NOT NULL,
                last_checked REAL NOT NULL DEFAULT 0
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                last_place TEXT NOT NULL DEFAULT ''
            )
            """
        )
        self._conn.commit()

    @staticmethod
    def _row_to_sub(row: sqlite3.Row) -> Subscription:
        return Subscription(
            id=row["id"],
            chat_id=row["chat_id"],
            mode=row["mode"],
            lat=row["lat"],
            lon=row["lon"],
            place=row["place"],
            fuel=row["fuel"],
            green_ids=json.loads(row["green_ids"]),
            created_at=row["created_at"],
            last_checked=row["last_checked"],
        )

    def add(
        self,
        chat_id: int,
        mode: str,
        lat: float,
        lon: float,
        place: str,
        fuel: str,
        green_ids: list[str],
    ) -> int | None:
        """Возвращает id подписки или None, если превышен лимит."""
        cur = self._conn.execute(
            "SELECT COUNT(*) AS n FROM subscriptions WHERE chat_id = ?", (chat_id,)
        )
        if cur.fetchone()["n"] >= MAX_SUBS_PER_CHAT:
            return None
        cur = self._conn.execute(
            """
            INSERT INTO subscriptions
                (chat_id, mode, lat, lon, place, fuel, green_ids, created_at, last_checked)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (chat_id, mode, lat, lon, place, fuel,
             json.dumps(green_ids), time.time(), time.time()),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def find(self, chat_id: int, place: str) -> Subscription | None:
        row = self._conn.execute(
            "SELECT * FROM subscriptions WHERE chat_id = ? AND place = ?",
            (chat_id, place),
        ).fetchone()
        return self._row_to_sub(row) if row else None

    def delete(self, sub_id: int) -> None:
        self._conn.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
        self._conn.commit()

    def list_for_chat(self, chat_id: int) -> list[Subscription]:
        rows = self._conn.execute(
            "SELECT * FROM subscriptions WHERE chat_id = ? ORDER BY created_at",
            (chat_id,),
        ).fetchall()
        return [self._row_to_sub(r) for r in rows]

    def all(self) -> list[Subscription]:
        rows = self._conn.execute("SELECT * FROM subscriptions").fetchall()
        return [self._row_to_sub(r) for r in rows]

    def update_green(self, sub_id: int, green_ids: list[str]) -> None:
        self._conn.execute(
            "UPDATE subscriptions SET green_ids = ?, last_checked = ? WHERE id = ?",
            (json.dumps(green_ids), time.time(), sub_id),
        )
        self._conn.commit()

    # ---------- отметки пользователей (свой краудсорсинг) ----------

    def add_report(self, station_id: str, place: str, status: str, chat_id: int) -> None:
        self._conn.execute(
            "INSERT INTO reports (station_id, place, status, chat_id, ts) VALUES (?, ?, ?, ?, ?)",
            (station_id, place, status, chat_id, time.time()),
        )
        self._conn.commit()

    def latest_reports(
        self, station_ids: list[str], max_age_sec: int = 6 * 3600
    ) -> dict[str, tuple[str, float, int]]:
        """Свежие отметки по станциям: station_id -> (статус, ts последней, всего отметок).

        Побеждает самая свежая отметка; количество — сколько всего отметок
        за окно (показываем как «подтверждения»).
        """
        if not station_ids:
            return {}
        cutoff = time.time() - max_age_sec
        placeholders = ",".join("?" for _ in station_ids)
        rows = self._conn.execute(
            f"SELECT station_id, status, ts FROM reports "
            f"WHERE ts > ? AND station_id IN ({placeholders}) ORDER BY ts",
            (cutoff, *station_ids),
        ).fetchall()
        out: dict[str, tuple[str, float, int]] = {}
        for row in rows:  # ORDER BY ts → последняя итерация = самая свежая
            sid = row["station_id"]
            prev = out.get(sid)
            out[sid] = (row["status"], row["ts"], (prev[2] if prev else 0) + 1)
        return out

    # ---------- подписки на цены ----------

    def add_price_sub(
        self, chat_id: int, mode: str, lat: float, lon: float,
        place: str, fuel: str, baseline: float,
    ) -> int | None:
        cur = self._conn.execute(
            "SELECT COUNT(*) AS n FROM price_subs WHERE chat_id = ?", (chat_id,)
        )
        if cur.fetchone()["n"] >= MAX_SUBS_PER_CHAT:
            return None
        cur = self._conn.execute(
            """
            INSERT INTO price_subs
                (chat_id, mode, lat, lon, place, fuel, baseline, created_at, last_checked)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (chat_id, mode, lat, lon, place, fuel, baseline,
             time.time(), time.time()),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def find_price_sub(self, chat_id: int, place: str, fuel: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM price_subs WHERE chat_id = ? AND place = ? AND fuel = ?",
            (chat_id, place, fuel),
        ).fetchone()

    def delete_price_sub(self, sub_id: int) -> None:
        self._conn.execute("DELETE FROM price_subs WHERE id = ?", (sub_id,))
        self._conn.commit()

    def list_price_subs(self, chat_id: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM price_subs WHERE chat_id = ? ORDER BY created_at",
            (chat_id,),
        ).fetchall()

    def all_price_subs(self) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM price_subs").fetchall()

    def update_price_baseline(self, sub_id: int, baseline: float) -> None:
        self._conn.execute(
            "UPDATE price_subs SET baseline = ?, last_checked = ? WHERE id = ?",
            (baseline, time.time(), sub_id),
        )
        self._conn.commit()

    def touch_price_check(self, sub_id: int) -> None:
        self._conn.execute(
            "UPDATE price_subs SET last_checked = ? WHERE id = ?",
            (time.time(), sub_id),
        )
        self._conn.commit()

    # ---------- пользователи и статистика ----------

    def touch_user(self, chat_id: int, place: str = "") -> None:
        now = time.time()
        self._conn.execute(
            """
            INSERT INTO users (chat_id, first_seen, last_seen, last_place)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                last_seen = excluded.last_seen,
                last_place = CASE WHEN excluded.last_place != ''
                                  THEN excluded.last_place
                                  ELSE users.last_place END
            """,
            (chat_id, now, now, place),
        )
        self._conn.commit()

    def get_stats(self) -> dict:
        now = time.time()
        day = now - 86400
        week = now - 7 * 86400

        def q(sql: str, args: tuple = ()) -> int:
            return int(self._conn.execute(sql, args).fetchone()[0])

        return {
            "total": q("SELECT COUNT(*) FROM users"),
            "new_today": q("SELECT COUNT(*) FROM users WHERE first_seen > ?", (day,)),
            "new_week": q("SELECT COUNT(*) FROM users WHERE first_seen > ?", (week,)),
            "active_24h": q("SELECT COUNT(*) FROM users WHERE last_seen > ?", (day,)),
            "subs": q("SELECT COUNT(*) FROM subscriptions"),
            "price_subs": q("SELECT COUNT(*) FROM price_subs"),
            "reports_24h": q("SELECT COUNT(*) FROM reports WHERE ts > ?", (day,)),
            "top_places": [
                (r["last_place"], r["n"])
                for r in self._conn.execute(
                    "SELECT last_place, COUNT(*) AS n FROM users "
                    "WHERE last_place != '' GROUP BY last_place "
                    "ORDER BY n DESC LIMIT 5"
                ).fetchall()
            ],
        }


storage = Storage()
