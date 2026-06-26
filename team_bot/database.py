import sqlite3
from datetime import date, datetime, timedelta
from typing import Optional
import json


class Database:
    def __init__(self, path="bot_data.db"):
        self.path = path
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS members (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS daily_absent (
                    member_id INTEGER,
                    date TEXT,
                    PRIMARY KEY (member_id, date)
                );

                CREATE TABLE IF NOT EXISTS attendance_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    member_id INTEGER,
                    date TEXT,
                    status TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(member_id, date)
                );

                CREATE TABLE IF NOT EXISTS report_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    user_name TEXT,
                    text TEXT,
                    date TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS warnings_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    member_id INTEGER,
                    date TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(member_id, date)
                );

                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT,
                    time TEXT,
                    topic_id INTEGER,
                    topic_key TEXT,
                    text TEXT,
                    enabled INTEGER DEFAULT 1,
                    is_builtin INTEGER DEFAULT 0
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_attendance_log_unique
                    ON attendance_log(member_id, date);

                CREATE UNIQUE INDEX IF NOT EXISTS idx_warnings_log_unique
                    ON warnings_log(member_id, date);

                CREATE TABLE IF NOT EXISTS member_status (
                    member_id INTEGER PRIMARY KEY,
                    status TEXT DEFAULT 'active',
                    status_until TEXT,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
            """)
            # Seed built-in notifications if empty
            cur = c.execute("SELECT COUNT(*) as cnt FROM notifications")
            if cur.fetchone()["cnt"] == 0:
                c.executemany(
                    "INSERT INTO notifications (id, name, time, topic_id, topic_key, text, enabled, is_builtin) VALUES (?,?,?,?,?,?,?,?)",
                    [
                        (1, "Утреннее напоминание (Git pull)", "10:00", None, "general",
                         "☀️ Не забудьте стянуть все изменения с Git!", 1, 1),
                        (2, "Посещаемость", "10:30", None, "work",
                         "Проверка посещаемости", 1, 1),
                        (3, "Вечернее напоминание", "20:00", None, "general",
                         "🌙 Не забудьте написать отчёт и запушить изменения!", 1, 1),
                        (4, "Анализ отчётов", "21:00", None, "reports",
                         "Анализ отчётов за день", 1, 1),
                    ]
                )

    # ── Members ──────────────────────────────────────────────────────────

    def get_members(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM members ORDER BY name").fetchall()
            return [dict(r) for r in rows]

    def add_member(self, user_id: int, name: str):
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO members (id, name) VALUES (?, ?)",
                (user_id, name)
            )

    def remove_member(self, user_id: int) -> str:
        with self._conn() as c:
            row = c.execute("SELECT name FROM members WHERE id=?", (user_id,)).fetchone()
            name = row["name"] if row else str(user_id)
            c.execute("DELETE FROM members WHERE id=?", (user_id,))
            return name

    # ── Attendance ───────────────────────────────────────────────────────

    def reset_daily_absent(self):
        today = date.today().isoformat()
        with self._conn() as c:
            c.execute("DELETE FROM daily_absent WHERE date=?", (today,))

    def toggle_absent(self, member_id: int) -> list[int]:
        today = date.today().isoformat()
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM daily_absent WHERE member_id=? AND date=?",
                (member_id, today)
            ).fetchone()
            if row:
                c.execute(
                    "DELETE FROM daily_absent WHERE member_id=? AND date=?",
                    (member_id, today)
                )
            else:
                c.execute(
                    "INSERT OR IGNORE INTO daily_absent (member_id, date) VALUES (?,?)",
                    (member_id, today)
                )
            rows = c.execute(
                "SELECT member_id FROM daily_absent WHERE date=?", (today,)
            ).fetchall()
            return [r["member_id"] for r in rows]

    def get_absent_ids(self) -> list[int]:
        today = date.today().isoformat()
        with self._conn() as c:
            rows = c.execute(
                "SELECT member_id FROM daily_absent WHERE date=?", (today,)
            ).fetchall()
            ids = [r["member_id"] for r in rows]
            absent_set = set(ids)
            # Log absent members
            for mid in ids:
                c.execute(
                    "INSERT OR IGNORE INTO attendance_log (member_id, date, status) VALUES (?,?,?)",
                    (mid, today, "absent")
                )
                c.execute(
                    "INSERT OR IGNORE INTO warnings_log (member_id, date) VALUES (?,?)",
                    (mid, today)
                )
            # Log present active members
            for m in self.get_active_members():
                if m["id"] not in absent_set:
                    c.execute(
                        "INSERT OR IGNORE INTO attendance_log (member_id, date, status) VALUES (?,?,?)",
                        (m["id"], today, "present")
                    )
            return ids

    # ── Reports ──────────────────────────────────────────────────────────

    def save_report_message(self, user_id: int, user_name: str, text: str, date: str):
        with self._conn() as c:
            c.execute(
                "INSERT INTO report_messages (user_id, user_name, text, date) VALUES (?,?,?,?)",
                (user_id, user_name, text, date)
            )

    def get_today_report_messages(self) -> list[dict]:
        today = date.today().isoformat()
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM report_messages WHERE date=? ORDER BY created_at",
                (today,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_all_report_dates(self) -> list[str]:
        """All distinct dates that have at least one report, ascending."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT DISTINCT date FROM report_messages ORDER BY date"
            ).fetchall()
            return [r["date"] for r in rows]

    def get_reporter_ids_for_date(self, date_str: str) -> set:
        """Set of user_ids who submitted a report on the given date."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT DISTINCT user_id FROM report_messages WHERE date=?", (date_str,)
            ).fetchall()
            return {r["user_id"] for r in rows}

    def get_reports_for_period(self, start_date: str = None, end_date: str = None) -> list[dict]:
        """Returns all report messages for the period, ordered by date then user."""
        if end_date is None:
            end_date = date.today().isoformat()
        with self._conn() as c:
            if start_date:
                rows = c.execute(
                    "SELECT * FROM report_messages WHERE date>=? AND date<=? ORDER BY date, user_name",
                    (start_date, end_date)
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM report_messages WHERE date<=? ORDER BY date, user_name",
                    (end_date,)
                ).fetchall()
            return [dict(r) for r in rows]

    # ── Notifications ────────────────────────────────────────────────────

    def get_notifications(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM notifications ORDER BY time").fetchall()
            return [dict(r) for r in rows]

    def get_notification(self, notif_id: int) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM notifications WHERE id=?", (notif_id,)).fetchone()
            return dict(row) if row else None

    def toggle_notification(self, notif_id: int):
        with self._conn() as c:
            c.execute(
                "UPDATE notifications SET enabled = 1 - enabled WHERE id=?",
                (notif_id,)
            )

    def add_notification(self, time_str: str, topic_id: int, topic_key: str, text: str) -> int:
        name = f"Кастомное {time_str}"
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO notifications (name, time, topic_id, topic_key, text, enabled, is_builtin) VALUES (?,?,?,?,?,1,0)",
                (name, time_str, topic_id, topic_key, text)
            )
            return cur.lastrowid

    def delete_notification(self, notif_id: int):
        with self._conn() as c:
            c.execute("DELETE FROM notifications WHERE id=? AND is_builtin=0", (notif_id,))

    # ── Member Status ────────────────────────────────────────────────────

    def set_member_status(self, member_id: int, status: str, until: str = None):
        with self._conn() as c:
            c.execute(
                """INSERT INTO member_status (member_id, status, status_until, updated_at)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(member_id) DO UPDATE SET
                       status = excluded.status,
                       status_until = excluded.status_until,
                       updated_at = excluded.updated_at""",
                (member_id, status, until)
            )

    def get_member_statuses(self) -> dict:
        """Returns dict of member_id -> status info. Auto-expires past dates."""
        today = date.today().isoformat()
        with self._conn() as c:
            rows = c.execute("SELECT * FROM member_status").fetchall()
        result = {}
        for r in rows:
            r = dict(r)
            if r["status"] != "active" and r["status_until"] and r["status_until"] < today:
                r["status"] = "active"
                r["status_until"] = None
            result[r["member_id"]] = r
        return result

    def get_active_members(self) -> list[dict]:
        """Members currently not on vacation or sick leave."""
        statuses = self.get_member_statuses()
        return [
            m for m in self.get_members()
            if statuses.get(m["id"], {}).get("status", "active") == "active"
        ]

    def get_today_summary(self) -> list[dict]:
        """Combined daily view: attendance + reports + leave status per member."""
        today = date.today().isoformat()
        statuses = self.get_member_statuses()
        members = self.get_members()

        with self._conn() as c:
            absent_ids = {
                r["member_id"] for r in c.execute(
                    "SELECT member_id FROM daily_absent WHERE date=?", (today,)
                ).fetchall()
            }
            attendance_map = {
                r["member_id"]: r["status"] for r in c.execute(
                    "SELECT member_id, status FROM attendance_log WHERE date=?", (today,)
                ).fetchall()
            }
            reported_ids = {
                r["user_id"] for r in c.execute(
                    "SELECT DISTINCT user_id FROM report_messages WHERE date=?", (today,)
                ).fetchall()
            }

        result = []
        for m in members:
            mid = m["id"]
            st = statuses.get(mid, {"status": "active", "status_until": None})
            if mid in absent_ids:
                attendance = "absent"
            elif mid in attendance_map:
                attendance = attendance_map[mid]
            else:
                attendance = "unknown"
            result.append({
                "id": mid,
                "name": m["name"],
                "status": st.get("status", "active"),
                "status_until": st.get("status_until"),
                "attendance": attendance,
                "reported": mid in reported_ids,
            })
        return result

    def get_streak(self, member_id: int) -> int:
        """Consecutive days with at least one report, counting back from today."""
        from datetime import date as ddate, timedelta as tdelta
        with self._conn() as c:
            rows = c.execute(
                "SELECT DISTINCT date FROM report_messages WHERE user_id=? ORDER BY date DESC",
                (member_id,)
            ).fetchall()
        dates = {r["date"] for r in rows}
        streak = 0
        day = ddate.today()
        while day.isoformat() in dates:
            streak += 1
            day -= tdelta(days=1)
        return streak

    def get_week_stats(self) -> dict:
        """Reports and absences per member for the current Mon–today."""
        from datetime import date as ddate, timedelta as tdelta
        today = ddate.today()
        monday = (today - tdelta(days=today.weekday())).isoformat()
        today_s = today.isoformat()
        with self._conn() as c:
            members = self.get_members()
            result = {}
            for m in members:
                mid = m["id"]
                reports = c.execute(
                    "SELECT COUNT(DISTINCT date) as n FROM report_messages "
                    "WHERE user_id=? AND date>=? AND date<=?",
                    (mid, monday, today_s)
                ).fetchone()["n"]
                absences = c.execute(
                    "SELECT COUNT(*) as n FROM attendance_log "
                    "WHERE member_id=? AND date>=? AND date<=? AND status!='present'",
                    (mid, monday, today_s)
                ).fetchone()["n"]
                result[mid] = {"reports": reports, "absences": absences, "name": m["name"]}
        return result

    def rename_member(self, member_id: int, new_name: str):
        with self._conn() as c:
            c.execute("UPDATE members SET name=? WHERE id=?", (new_name, member_id))

    def log_attendance(self, member_id: int, date_str: str, status: str):
        """Insert or replace attendance record. status: present|sick|vacation|absent."""
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO attendance_log (member_id, date, status) VALUES (?,?,?)",
                (member_id, date_str, status)
            )

    def get_attendance_history(self, member_id: int, days: int = 14) -> list[dict]:
        cutoff = (datetime.now() - timedelta(days=days)).date().isoformat()
        with self._conn() as c:
            rows = c.execute(
                "SELECT date, status FROM attendance_log WHERE member_id=? AND date>=? ORDER BY date",
                (member_id, cutoff)
            ).fetchall()
            return [dict(r) for r in rows]

    def find_member(self, id_or_name: str) -> Optional[dict]:
        """Find a member by numeric ID or exact name (case-insensitive)."""
        members = self.get_members()
        try:
            uid = int(id_or_name)
            return next((m for m in members if m["id"] == uid), None)
        except ValueError:
            return next((m for m in members if m["name"].lower() == id_or_name.lower()), None)

    # ── Analytics ────────────────────────────────────────────────────────

    def get_analytics(self, start_date: str = None, end_date: str = None) -> dict:
        if end_date is None:
            end_date = date.today().isoformat()
        with self._conn() as c:
            members = self.get_members()
            result = {}
            for m in members:
                mid = m["id"]
                if start_date:
                    absences = c.execute(
                        "SELECT COUNT(*) as cnt FROM attendance_log "
                        "WHERE member_id=? AND date>=? AND date<=? AND status='absent'",
                        (mid, start_date, end_date)
                    ).fetchone()["cnt"]
                    reports = c.execute(
                        "SELECT COUNT(DISTINCT date) as cnt FROM report_messages "
                        "WHERE user_id=? AND date>=? AND date<=?",
                        (mid, start_date, end_date)
                    ).fetchone()["cnt"]
                    warnings = c.execute(
                        "SELECT COUNT(*) as cnt FROM warnings_log "
                        "WHERE member_id=? AND date>=? AND date<=?",
                        (mid, start_date, end_date)
                    ).fetchone()["cnt"]
                else:
                    absences = c.execute(
                        "SELECT COUNT(*) as cnt FROM attendance_log "
                        "WHERE member_id=? AND status='absent'",
                        (mid,)
                    ).fetchone()["cnt"]
                    reports = c.execute(
                        "SELECT COUNT(DISTINCT date) as cnt FROM report_messages "
                        "WHERE user_id=?",
                        (mid,)
                    ).fetchone()["cnt"]
                    warnings = c.execute(
                        "SELECT COUNT(*) as cnt FROM warnings_log "
                        "WHERE member_id=?",
                        (mid,)
                    ).fetchone()["cnt"]
                result[mid] = {
                    "absences": absences,
                    "reports": reports,
                    "warnings": warnings
                }
            return result

    def get_weekly_analytics(self, start_date: str, end_date: str) -> dict:
        with self._conn() as c:
            members = self.get_members()
            result = {}
            for m in members:
                mid = m["id"]
                absences = c.execute(
                    "SELECT COUNT(*) as cnt FROM attendance_log "
                    "WHERE member_id=? AND date>=? AND date<=? AND status='absent'",
                    (mid, start_date, end_date)
                ).fetchone()["cnt"]
                reports = c.execute(
                    "SELECT COUNT(DISTINCT date) as cnt FROM report_messages "
                    "WHERE user_id=? AND date>=? AND date<=?",
                    (mid, start_date, end_date)
                ).fetchone()["cnt"]
                result[mid] = {"absences": absences, "reports": reports}
            return result


db = Database()
