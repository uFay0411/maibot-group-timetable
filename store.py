"""SQLite 持久化；所有同步操作由插件在工作线程执行。"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import List
from zoneinfo import ZoneInfo

from icalendar import Calendar

import json
import sqlite3

from .calendar_engine import Lesson, apply_change, expand, parse_calendar
from .matching import match_subjects


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS members (
                owner TEXT PRIMARY KEY, stream TEXT NOT NULL, user TEXT NOT NULL,
                name TEXT NOT NULL, zone TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS courses (
                owner TEXT NOT NULL, id TEXT NOT NULL, ics TEXT NOT NULL,
                patch TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(owner,id));
            CREATE TABLE IF NOT EXISTS changes (
                owner TEXT NOT NULL, id TEXT NOT NULL, original TEXT NOT NULL,
                data TEXT NOT NULL, PRIMARY KEY(owner,id,original));
            CREATE TABLE IF NOT EXISTS uploads (
                owner TEXT PRIMARY KEY, name TEXT NOT NULL, content BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS deliveries (
                key TEXT PRIMARY KEY, owner TEXT NOT NULL, state TEXT NOT NULL,
                updated REAL NOT NULL, task_id TEXT NOT NULL DEFAULT '');
            """)
            # 上次进程若停在生成阶段，尚未调用发送接口，可安全恢复。
            db.execute("DELETE FROM deliveries WHERE state='generating'")

    def connect(self):
        # Connection 的 context manager 只提交事务，不关闭连接。
        return closing_connection(self.path)

    def member(self, owner: str, stream: str, user: str, name: str, zone: str):
        with self.connect() as db:
            db.execute("INSERT INTO members VALUES (?,?,?,?,?,1) ON CONFLICT(owner) DO UPDATE SET "
                       "stream=excluded.stream,name=excluded.name", (owner, stream, user, name, zone))

    def get_member(self, owner: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM members WHERE owner=?", (owner,)).fetchone()
            if row is None:
                raise ValueError("请先发送 /课表 帮助")
            return dict(row)

    def members(self) -> List[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM members WHERE enabled=1")]

    def group_members(self, platform: str, group_id: str, stream: str) -> List[dict]:
        # enabled 仅控制个人提醒，群内只读查询必须包含关闭提醒的成员。
        with self.connect() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM members WHERE stream=?", (stream,))]
        result = []
        for row in rows:
            try:
                owner = json.loads(row["owner"])
            except (ValueError, TypeError):
                continue
            if isinstance(owner, list) and len(owner) == 3 and owner == [platform, group_id, row["user"]]:
                result.append(row)
        return result

    def setting(self, owner: str, field: str, value):
        if field not in {"zone", "enabled"}:
            raise ValueError("未知设置")
        with self.connect() as db:
            db.execute(f"UPDATE members SET {field}=? WHERE owner=?", (value, owner))

    def upload(self, owner: str, name: str, raw: bytes):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO uploads VALUES (?,?,?)", (owner, name, raw))

    def import_latest(self, owner: str, mode: str):
        with self.connect() as db:
            row = db.execute("SELECT content FROM uploads WHERE owner=?", (owner,)).fetchone()
        if row is None:
            raise ValueError("请先在本群上传自己的 .ics 文件")
        return self.import_ics(owner, row[0], mode)

    def import_ics(self, owner: str, raw: bytes, mode: str, upload_name: str | None = None):
        if mode not in {"合并", "替换"}:
            raise ValueError("解析模式为 合并 或 替换")
        courses, skipped = parse_calendar(raw, self.get_member(owner)["zone"])
        # 提前验证展开，失败不改旧课表。仅允许课程级、低频重复规则。
        for ics in courses.values():
            if any(f"FREQ={freq}" in ics.upper() for freq in ("SECONDLY", "MINUTELY", "HOURLY")):
                raise ValueError("课表不支持小时、分钟或秒级重复")
        now = datetime.now(ZoneInfo(self.get_member(owner)["zone"]))
        for cid, ics in courses.items():
            expand(cid, ics, now, now + timedelta(days=14), self.get_member(owner)["zone"])
        with self.connect() as db:
            if mode == "替换":
                db.execute("DELETE FROM courses WHERE owner=?", (owner,))
                db.execute("DELETE FROM changes WHERE owner=?", (owner,))
            for cid, ics in courses.items():
                # 合并更新原始日历，但保留用户的本地调课与整门课程设置。
                db.execute("INSERT INTO courses(owner,id,ics) VALUES (?,?,?) "
                           "ON CONFLICT(owner,id) DO UPDATE SET ics=excluded.ics", (owner, cid, ics))
            if upload_name is not None:
                db.execute("INSERT OR REPLACE INTO uploads VALUES (?,?,?)", (owner, upload_name, raw))
        return len(courses), skipped

    def lessons(self, owner: str, lower: datetime, upper: datetime, *, overlapping: bool = False) -> List[Lesson]:
        zone = self.get_member(owner)["zone"]
        with self.connect() as db:
            courses = list(db.execute("SELECT * FROM courses WHERE owner=?", (owner,)))
            changes = {(r["id"], r["original"]): json.loads(r["data"]) for r in
                       db.execute("SELECT * FROM changes WHERE owner=?", (owner,))}
        result = {}
        for row in courses:
            cid = row["id"]
            patch = json.loads(row["patch"])
            if patch.get("deleted"):
                continue
            for lesson in expand(cid, row["ics"], lower, upper, zone, overlapping=overlapping):
                change = changes.get((cid, lesson.original), {})
                if not change.get("cancelled"):
                    changed = apply_change(apply_change(lesson, patch, zone), change, zone)
                    included = changed.start < upper and changed.end > lower if overlapping else lower <= changed.start < upper
                    if included:
                        result[(cid, lesson.original)] = changed
            # 调到查询窗口里的课，即使原时间在窗口外，也必须查到。
            for (change_cid, original), change in changes.items():
                if change_cid != cid or change.get("cancelled") or "start" not in change:
                    continue
                moved = datetime.fromisoformat(change["start"])
                if overlapping or lower <= moved < upper:
                    origin = datetime.fromisoformat(change.get("source_start", original))
                    originals = expand(cid, row["ics"], origin - timedelta(seconds=1),
                                       origin + timedelta(seconds=1), zone)
                    originals = [x for x in originals if x.original == original]
                    if originals:
                        changed = apply_change(apply_change(originals[0], patch, zone), change, zone)
                        included = changed.start < upper and changed.end > lower if overlapping else lower <= changed.start < upper
                        if included:
                            result[(cid, original)] = changed
        return sorted(result.values(), key=lambda x: (x.start, x.course_id))

    def select(self, owner: str, token: str) -> Lesson:
        try:
            cid, timepart = token.split("@")
            zone = self.get_member(owner)["zone"]
            start = datetime.strptime(timepart, "%Y%m%dT%H%M").replace(tzinfo=ZoneInfo(zone))
        except (ValueError, TypeError) as exc:
            raise ValueError("请使用查询结果里的完整课次编号，如 abc123@20261026T0800") from exc
        matches = [x for x in self.lessons(owner, start, start + timedelta(minutes=1))
                   if x.course_id == cid]
        if len(matches) != 1:
            raise ValueError("找不到该课次，请重新查询课表取得最新编号")
        return matches[0]

    def change(self, owner: str, lesson: Lesson, data: dict):
        with self.connect() as db:
            previous = db.execute("SELECT data FROM changes WHERE owner=? AND id=? AND original=?",
                                  (owner, lesson.course_id, lesson.original)).fetchone()
            merged = json.loads(previous[0]) if previous else {}
            merged.setdefault("source_start", lesson.start.isoformat())
            merged.update(data)
            db.execute("INSERT OR REPLACE INTO changes VALUES (?,?,?,?)",
                       (owner, lesson.course_id, lesson.original, json.dumps(merged, ensure_ascii=False)))

    def patch(self, owner: str, cid: str, data: dict):
        with self.connect() as db:
            row = db.execute("SELECT patch FROM courses WHERE owner=? AND id=?", (owner, cid)).fetchone()
            if row is None:
                raise ValueError("找不到你的课程编号")
            patch = json.loads(row[0])
            patch.update(data)
            db.execute("UPDATE courses SET patch=? WHERE owner=? AND id=?",
                       (json.dumps(patch, ensure_ascii=False), owner, cid))

    def restore(self, owner: str, cid: str):
        self.patch(owner, cid, {})  # 先校验归属。
        with self.connect() as db:
            db.execute("UPDATE courses SET patch='{}' WHERE owner=? AND id=?", (owner, cid))
            db.execute("DELETE FROM changes WHERE owner=? AND id=?", (owner, cid))

    def course_list(self, owner: str, subject: str = "") -> List[str]:
        courses = self.course_details(owner)
        if subject.strip():
            names = {x["subject"] for x in match_subjects(subject, (x["subject"] for x in courses))}
            courses = [x for x in courses if x["subject"] in names]
        return [f"{x['course_id']} {x['subject']} {'[已停用]' if x['deleted'] else ''}"
                for x in courses]

    def course_details(self, owner: str) -> List[dict]:
        with self.connect() as db:
            rows = list(db.execute("SELECT * FROM courses WHERE owner=? ORDER BY id", (owner,)))
        result = []
        for row in rows:
            events = Calendar.from_ical(row["ics"]).walk("VEVENT")
            patch = json.loads(row["patch"])
            title = patch.get("title", str(events[0].get("SUMMARY", "已取消课程")))
            room = patch.get("room", str(events[0].get("LOCATION", "")))
            result.append({"course_id": row["id"], "subject": title, "classroom": room or None,
                           "deleted": bool(patch.get("deleted"))})
        return result

    def clear(self, owner: str):
        with self.connect() as db:
            for table in ("courses", "changes", "uploads"):
                db.execute(f"DELETE FROM {table} WHERE owner=?", (owner,))

    def claim(self, key: str, owner: str, now: float) -> bool:
        with self.connect() as db:
            return db.execute("INSERT OR IGNORE INTO deliveries VALUES (?,?, 'generating',?,'')",
                              (key, owner, now)).rowcount == 1

    def sending(self, keys: List[str], now: float):
        with self.connect() as db:
            for key in keys:
                db.execute("UPDATE deliveries SET state='pending',updated=? WHERE key=?", (now, key))

    def finish(self, key: str, task: str, now: float):
        with self.connect() as db:
            db.execute("UPDATE deliveries SET state='sent',task_id=?,updated=? WHERE key=?", (task, now, key))

    def release(self, key: str):
        with self.connect() as db:
            db.execute("DELETE FROM deliveries WHERE key=? AND state IN ('generating','pending')", (key,))

    def delivery_status(self, owner: str) -> dict:
        with self.connect() as db:
            return {r[0]: r[1] for r in db.execute(
                "SELECT state,count(*) FROM deliveries WHERE owner=? GROUP BY state", (owner,))}


class closing_connection:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        return self.db

    def __exit__(self, kind, value, traceback):
        try:
            self.db.rollback() if kind else self.db.commit()
        finally:
            self.db.close()
