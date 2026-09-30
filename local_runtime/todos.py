"""User-owned, structured to-do lists. SQL is authoritative; no vector top-k."""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .access import viewer

STATUSES = ("draft", "pending", "in_progress", "blocked", "done", "cancelled")
ACTIVE = ("pending", "in_progress", "blocked")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def zone(value):
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise HTTPException(422, "请填写有效的 IANA 时区，例如 Asia/Shanghai") from None


def instant(value):
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (ValueError, OverflowError):
        raise HTTPException(422, "时间须为带时区的 ISO 日期时间") from None


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(default="", max_length=64)
    text: str = Field(min_length=1, max_length=300)
    done: bool = False


class TodoData(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(default="", max_length=6000)
    list_name: str = Field(default="默认清单", min_length=1, max_length=80)
    status: Literal["draft", "pending", "in_progress", "blocked", "done", "cancelled"] = "pending"
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    family_id: str = Field(default="", max_length=64)
    device_id: str = Field(default="", max_length=64)
    visibility: Literal["personal", "family"] = "personal"
    assignee_user_id: str = Field(default="", max_length=64)
    start_at: str = Field(default="", max_length=40)
    due_at: str = Field(default="", max_length=40)
    due_date: str = Field(default="", max_length=10)
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=80)
    location: str = Field(default="", max_length=240)
    estimated_minutes: int | None = Field(default=None, ge=1, le=10080)
    tags: list[str] = Field(default_factory=list, max_length=10)
    checklist: list[Item] = Field(default_factory=list, max_length=50)


class CreateInput(TodoData):
    request_id: str = Field(default="", max_length=128)


class UpdateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    changes: dict


class RevisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)


class PurgeInput(RevisionInput):
    title: str


class Todos:
    def __init__(self, service):
        self.service, self.access = service, service.access
        with self.access.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS todos (
                  id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL,
                  family_id TEXT NOT NULL, device_id TEXT NOT NULL, visibility TEXT NOT NULL,
                  assignee_user_id TEXT NOT NULL, list_name TEXT NOT NULL, title TEXT NOT NULL,
                  status TEXT NOT NULL, priority TEXT NOT NULL, due_at TEXT NOT NULL,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  completed_at TEXT NOT NULL, cancelled_at TEXT NOT NULL,
                  revision INTEGER NOT NULL, data TEXT NOT NULL, source TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS todo_private ON todos(owner_user_id,visibility,status,due_at);
                CREATE INDEX IF NOT EXISTS todo_shared ON todos(family_id,visibility,status,due_at);
                CREATE TABLE IF NOT EXISTS todo_events (
                  id INTEGER PRIMARY KEY, todo_id TEXT NOT NULL, revision INTEGER NOT NULL,
                  actor TEXT NOT NULL, action TEXT NOT NULL, changes TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS todo_event_record ON todo_events(todo_id,revision);
                CREATE TABLE IF NOT EXISTS todo_requests (
                  user_id TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                  todo_id TEXT NOT NULL, PRIMARY KEY(user_id,request_id));
                CREATE TABLE IF NOT EXISTS todo_delete_confirmations (
                  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, todo_id TEXT NOT NULL,
                  revision INTEGER NOT NULL, session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                  expires_at TEXT NOT NULL, consumed_at TEXT NOT NULL DEFAULT '',
                  UNIQUE(user_id,todo_id,revision,turn_id));
            """)
            columns = {r[1] for r in db.execute("PRAGMA table_info(todos)")}
            for name in ("deleted_at", "deleted_by"):
                if name not in columns:
                    db.execute(f"ALTER TABLE todos ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def visibility_sql(user):
        return (
            "((visibility='personal' AND owner_user_id=?) OR (visibility='family' AND family_id IN "
            "(SELECT family_id FROM app_members WHERE user_id=?)))",
            [user, user],
        )

    def _record(self, db, user, task_id):
        where, args = self.visibility_sql(user)
        row = db.execute("SELECT * FROM todos WHERE id=? AND " + where, [task_id, *args]).fetchone()
        if row is None:
            raise HTTPException(404, "待办不存在或当前用户无权访问")
        return row

    def _item(self, db, row, user):
        item = json.loads(row["data"])
        item.update(
            {
                key: row[key]
                for key in (
                    "id",
                    "owner_user_id",
                    "created_at",
                    "updated_at",
                    "completed_at",
                    "cancelled_at",
                    "revision",
                    "deleted_at",
                    "deleted_by",
                )
            }
        )
        owner = db.execute("SELECT owner_user_id FROM app_families WHERE family_id=?", (row["family_id"],)).fetchone()
        manage = row["owner_user_id"] == user or (row["visibility"] == "family" and owner and owner[0] == user)
        item.update(
            can_manage=bool(manage),
            can_edit=bool(not row["deleted_at"] and (manage or row["assignee_user_id"] == user)),
            can_delete=bool(manage),
            overdue=not row["deleted_at"] and row["status"] in ACTIVE and bool(row["due_at"]) and row["due_at"] < now(),
        )
        source = json.loads(row["source"])
        item["source"] = source if row["owner_user_id"] == user else {"type": source.get("type", "manual")}
        names = {
            r["user_id"]: r["name"]
            for r in db.execute(
                "SELECT * FROM app_users WHERE user_id IN (?,?)", (row["owner_user_id"], row["assignee_user_id"])
            )
        }
        item["owner_name"] = names.get(row["owner_user_id"], row["owner_user_id"])
        item["assignee_name"] = names.get(row["assignee_user_id"], row["assignee_user_id"])
        return item

    def validate(self, db, user, body, *, creating=False):
        try:
            value = TodoData.model_validate(body).model_dump()
        except ValidationError as exc:
            raise HTTPException(422, "待办字段无效：" + ", ".join(str(e["loc"][0]) for e in exc.errors())) from None
        tz = zone(value["timezone"])
        if creating:
            if user == "knowin_public":
                raise HTTPException(403, "公共资料身份只读，请选择个人用户")
            self.access.require_context(user, value["family_id"], value["device_id"])
        if value["visibility"] == "family" and not value["family_id"]:
            raise HTTPException(422, "家庭共享待办必须选择家庭")
        assignee = value["assignee_user_id"]
        if value["visibility"] == "personal":
            if assignee and assignee != user:
                raise HTTPException(422, "私人待办只能指派给自己")
            value["assignee_user_id"] = user
        elif (
            assignee
            and not db.execute(
                "SELECT 1 FROM app_members WHERE family_id=? AND user_id=?", (value["family_id"], assignee)
            ).fetchone()
        ):
            raise HTTPException(422, "负责人必须是该家庭的现有成员")
        value["start_at"] = instant(value["start_at"])
        if value["due_date"]:
            if value["due_at"]:
                raise HTTPException(422, "截止日期和精确截止时间不能同时填写")
            try:
                day = date.fromisoformat(value["due_date"])
                value["due_date"] = day.isoformat()
                deadline = datetime.combine(day + timedelta(days=1), time.min, tz) - timedelta(microseconds=1)
                due_sort = instant(deadline.isoformat())
            except (ValueError, OverflowError):
                raise HTTPException(422, "截止日期格式应为 YYYY-MM-DD") from None
        else:
            value["due_at"] = instant(value["due_at"])
            due_sort = value["due_at"]
        if value["start_at"] and due_sort and value["start_at"] > due_sort:
            raise HTTPException(422, "计划开始时间不能晚于截止时间")
        tags = list(dict.fromkeys(t.strip() for t in value["tags"] if t.strip()))
        if any(len(t) > 40 for t in tags):
            raise HTTPException(422, "每个标签不能超过 40 字符")
        value["tags"] = tags
        seen = set()
        for item in value["checklist"]:
            item["id"] = item["id"] or str(uuid.uuid4())
            if item["id"] in seen:
                raise HTTPException(422, "子任务 ID 不能重复")
            seen.add(item["id"])
        return value, due_sort

    @staticmethod
    def event(db, task_id, revision, actor, action, changes):
        db.execute(
            "INSERT INTO todo_events(todo_id,revision,actor,action,changes,created_at) VALUES (?,?,?,?,?,?)",
            (task_id, revision, actor, action, json.dumps(changes, ensure_ascii=False), now()),
        )

    def get(self, user, task_id):
        with self.access.db() as db:
            item = self._item(db, self._record(db, user, task_id), user)
            item["events"] = [
                {**dict(r), "changes": json.loads(r["changes"])}
                for r in db.execute("SELECT * FROM todo_events WHERE todo_id=? ORDER BY revision DESC", (task_id,))
            ]
            return item

    def create(self, user, body, *, request_id="", source=None):
        # Fingerprint input before generated checklist IDs. A retry remains the same request.
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.service.memory_lock, self.access.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if request_id:
                replay = db.execute(
                    "SELECT * FROM todo_requests WHERE user_id=? AND request_id=?", (user, request_id)
                ).fetchone()
                if replay:
                    if replay["fingerprint"] != fingerprint:
                        raise HTTPException(409, "此请求 ID 已用于不同内容，请重新提交")
                    if not db.execute(
                        "SELECT 1 FROM todos WHERE id=? AND deleted_at=''", (replay["todo_id"],)
                    ).fetchone():
                        raise HTTPException(410, "该请求创建的待办已删除，不能通过重试恢复")
                    return {**self._item(db, self._record(db, user, replay["todo_id"]), user), "deduplicated": True}
            value, due = self.validate(db, user, body, creating=True)
            task_id, timestamp = str(uuid.uuid4()), now()
            completed, cancelled = (
                (timestamp if value["status"] == "done" else ""),
                (timestamp if value["status"] == "cancelled" else ""),
            )
            db.execute(
                """INSERT INTO todos(id,owner_user_id,family_id,device_id,visibility,assignee_user_id,
                list_name,title,status,priority,due_at,created_at,updated_at,completed_at,cancelled_at,
                revision,data,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id,
                    user,
                    *[
                        value[k]
                        for k in (
                            "family_id",
                            "device_id",
                            "visibility",
                            "assignee_user_id",
                            "list_name",
                            "title",
                            "status",
                            "priority",
                        )
                    ],
                    due,
                    timestamp,
                    timestamp,
                    completed,
                    cancelled,
                    1,
                    json.dumps(value, ensure_ascii=False),
                    json.dumps(source or {"type": "manual"}, ensure_ascii=False),
                ),
            )
            if request_id:
                db.execute("INSERT INTO todo_requests VALUES (?,?,?,?)", (user, request_id, fingerprint, task_id))
            self.event(db, task_id, 1, user, "created", value)
            return self._item(db, self._record(db, user, task_id), user)

    def update(self, user, task_id, revision, changes):
        allowed = set(TodoData.model_fields) - {"family_id", "device_id", "visibility"}
        if not changes or set(changes) - allowed:
            raise HTTPException(422, "更新字段无效；所属家庭、设备和共享范围创建后不可更改")
        with self.service.memory_lock, self.access.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._record(db, user, task_id)
            if row["deleted_at"]:
                raise HTTPException(409, "待办在回收站中，请先恢复")
            permissions = self._item(db, row, user)
            if not permissions["can_edit"] or (
                not permissions["can_manage"] and set(changes) - {"status", "checklist"}
            ):
                raise HTTPException(403, "负责人可更新进度；其他字段需创建者或家庭管理员修改")
            if row["revision"] != revision:
                raise HTTPException(409, "待办已被修改，请刷新详情后重试")
            old = json.loads(row["data"])
            merged = {**old, **changes}
            # Also recover pre-upgrade assignments whose member was removed by
            # an older client. Explicitly choosing an invalid new assignee fails.
            if (
                row["visibility"] == "family"
                and "assignee_user_id" not in changes
                and old["assignee_user_id"]
                and not db.execute(
                    "SELECT 1 FROM app_members WHERE family_id=? AND user_id=?",
                    (row["family_id"], old["assignee_user_id"]),
                ).fetchone()
            ):
                merged["assignee_user_id"] = ""
            value, due = self.validate(db, row["owner_user_id"], merged)
            diff = {k: {"before": old[k], "after": value[k]} for k in value if old[k] != value[k]}
            if not diff:
                return permissions
            timestamp = now()
            completed = (row["completed_at"] or timestamp) if value["status"] == "done" else ""
            cancelled = (row["cancelled_at"] or timestamp) if value["status"] == "cancelled" else ""
            db.execute(
                """UPDATE todos SET assignee_user_id=?,list_name=?,title=?,status=?,priority=?,due_at=?,updated_at=?,
                completed_at=?,cancelled_at=?,revision=revision+1,data=? WHERE id=?""",
                (
                    *[value[k] for k in ("assignee_user_id", "list_name", "title", "status", "priority")],
                    due,
                    timestamp,
                    completed,
                    cancelled,
                    json.dumps(value, ensure_ascii=False),
                    task_id,
                ),
            )
            self.event(db, task_id, revision + 1, user, "updated", diff)
            return self._item(db, self._record(db, user, task_id), user)

    def delete(self, user, task_id, revision):
        with self.service.memory_lock, self.access.db() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._trash(db, user, task_id, revision)

    def _trash(self, db, user, task_id, revision):
        row = self._record(db, user, task_id)
        self._manage(db, row, user, revision)
        if row["deleted_at"]:
            raise HTTPException(409, "待办已在回收站中")
        stamp = now()
        db.execute(
            "UPDATE todos SET deleted_at=?,deleted_by=?,updated_at=?,revision=revision+1 WHERE id=?",
            (stamp, user, stamp, task_id),
        )
        self.event(db, task_id, revision + 1, user, "trashed", {})
        return {"deleted": True, "trashed": True, "id": task_id, "revision": revision + 1}

    def _manage(self, db, row, user, revision):
        if not self._item(db, row, user)["can_delete"]:
            raise HTTPException(403, "只有创建者或家庭管理员可管理回收站")
        if row["revision"] != revision:
            raise HTTPException(409, "待办已被修改，请刷新后重试")

    def restore(self, user, task_id, revision):
        with self.service.memory_lock, self.access.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._record(db, user, task_id)
            self._manage(db, row, user, revision)
            if not row["deleted_at"]:
                raise HTTPException(409, "待办不在回收站中")
            db.execute(
                "UPDATE todos SET deleted_at='',deleted_by='',updated_at=?,revision=revision+1 WHERE id=?",
                (now(), task_id),
            )
            self.event(db, task_id, revision + 1, user, "restored", {})
            return self._item(db, self._record(db, user, task_id), user)

    def purge(self, user, task_id, revision, title):
        with self.service.memory_lock, self.access.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._record(db, user, task_id)
            self._manage(db, row, user, revision)
            if not row["deleted_at"] or title != row["title"]:
                raise HTTPException(422, "只能彻底删除回收站中的待办，请输入完整标题确认")
            db.execute("DELETE FROM todo_delete_confirmations WHERE todo_id=?", (task_id,))
            db.execute("DELETE FROM todo_events WHERE todo_id=?", (task_id,))
            db.execute("DELETE FROM todos WHERE id=?", (task_id,))
        return {"deleted": True, "purged": True, "id": task_id}

    def propose_delete(self, user, task_id, revision, session_id, turn_id):
        with self.service.memory_lock, self.access.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._record(db, user, task_id)
            self._manage(db, row, user, revision)
            if row["deleted_at"]:
                raise HTTPException(409, "待办已在回收站中")
            db.execute("DELETE FROM todo_delete_confirmations WHERE expires_at<?", (now(),))
            expiry = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(timespec="microseconds")
            db.execute(
                "INSERT OR IGNORE INTO todo_delete_confirmations VALUES (?,?,?,?,?,?,?,'')",
                (secrets.token_urlsafe(24), user, task_id, revision, session_id, turn_id, expiry),
            )
            request = db.execute(
                "SELECT * FROM todo_delete_confirmations WHERE user_id=? AND todo_id=? AND revision=? AND turn_id=?",
                (user, task_id, revision, turn_id),
            ).fetchone()
            return {
                "requires_confirmation": True,
                "confirmation_id": request["id"],
                "expires_at": request["expires_at"],
                "todo": self._item(db, row, user),
                "message": "尚未删除。请在确认卡上点击移入回收站；纯文字回复不会执行删除。",
            }

    def confirm_delete(self, user, confirmation_id):
        with self.service.memory_lock, self.access.db() as db:
            db.execute("BEGIN IMMEDIATE")
            request = db.execute(
                "SELECT * FROM todo_delete_confirmations WHERE id=? AND user_id=?", (confirmation_id, user)
            ).fetchone()
            if not request:
                raise HTTPException(404, "确认请求不存在或无权访问")
            if request["expires_at"] < now():
                raise HTTPException(410, "确认已过期，请重新发起删除")
            if not db.execute(
                """SELECT 1 FROM chat_turns t JOIN chat_sessions s ON s.id=t.session_id
                WHERE t.id=? AND s.id=? AND s.user_id=?""",
                (request["turn_id"], request["session_id"], user),
            ).fetchone():
                raise HTTPException(410, "原始对话已删除，确认已失效")
            row = self._record(db, user, request["todo_id"])
            if request["consumed_at"]:
                return {"already_confirmed": True, "id": row["id"], "trashed": bool(row["deleted_at"])}
            result = self._trash(db, user, row["id"], request["revision"])
            db.execute("UPDATE todo_delete_confirmations SET consumed_at=? WHERE id=?", (now(), confirmation_id))
            return result

    def cancel_delete(self, user, confirmation_id):
        with self.service.memory_lock, self.access.db() as db:
            db.execute("BEGIN IMMEDIATE")
            request = db.execute(
                "SELECT consumed_at FROM todo_delete_confirmations WHERE id=? AND user_id=?", (confirmation_id, user)
            ).fetchone()
            if not request:
                raise HTTPException(404, "确认已取消或失效")
            if request["consumed_at"]:
                raise HTTPException(409, "此操作已确认，不能取消；如需撤销删除，请到回收站恢复")
            db.execute(
                "DELETE FROM todo_delete_confirmations WHERE id=? AND user_id=? AND consumed_at=''",
                (confirmation_id, user),
            )
        return {"cancelled": True}

    def deletion_status(self, user, confirmation_id):
        with self.access.db() as db:
            request = db.execute(
                "SELECT * FROM todo_delete_confirmations WHERE id=? AND user_id=?", (confirmation_id, user)
            ).fetchone()
            if not request:
                raise HTTPException(404, "确认已取消或失效")
            row = self._record(db, user, request["todo_id"])
            state = (
                "confirmed"
                if request["consumed_at"]
                else "expired"
                if request["expires_at"] < now()
                else "stale"
                if row["revision"] != request["revision"]
                else "pending"
            )
            return {"state": state, "trashed": bool(row["deleted_at"])}

    def list(
        self,
        user,
        *,
        family_id="",
        device_id="",
        status="all",
        period="all",
        priority="",
        assignee_user_id="",
        list_name="",
        q="",
        page=1,
        page_size=20,
        timezone_name="Asia/Shanghai",
        trashed=False,
    ):
        if status not in {"all", "open", *STATUSES} or period not in {
            "all",
            "today",
            "upcoming",
            "overdue",
            "unscheduled",
        }:
            raise HTTPException(422, "待办筛选条件无效")
        if not 1 <= page_size <= 100 or page < 1 or len(q) > 500:
            raise HTTPException(422, "分页或搜索条件无效")
        tz = zone(timezone_name)
        where, args = self.visibility_sql(user)
        where += " AND deleted_at<>''" if trashed else " AND deleted_at=''"
        for key, value in (
            ("family_id", family_id),
            ("device_id", device_id),
            ("priority", priority),
            ("assignee_user_id", assignee_user_id),
            ("list_name", list_name),
        ):
            if value:
                where += " AND " + key + "=?"
                args.append("" if value == "__none__" else value)
        if status == "open":
            where += " AND status IN ('draft','pending','in_progress','blocked')"
        elif status != "all":
            where += " AND status=?"
            args.append(status)
        if q:
            where += (
                " AND (instr(lower(title),lower(?))>0 OR instr(lower(json_extract(data,'$.description')),lower(?))>0)"
            )
            args += [q, q]
        current = datetime.now(timezone.utc)
        midnight = datetime.combine(current.astimezone(tz).date(), time.min, tz)
        if period == "overdue":
            where += " AND status IN ('pending','in_progress','blocked') AND due_at<>'' AND due_at<?"
            args.append(instant(current.isoformat()))
        elif period in {"today", "upcoming"}:
            begin = midnight if period == "today" else midnight + timedelta(days=1)
            end = midnight + timedelta(days=1 if period == "today" else 8)
            where += " AND due_at>=? AND due_at<?"
            args += [instant(begin.isoformat()), instant(end.isoformat())]
        elif period == "unscheduled":
            where += " AND due_at=''"
        with self.access.db() as db:
            total = db.execute("SELECT COUNT(*) FROM todos WHERE " + where, args).fetchone()[0]
            rows = db.execute(
                "SELECT * FROM todos WHERE "
                + where
                + " ORDER BY CASE WHEN status IN ('done','cancelled') THEN 1 ELSE 0 END,"
                "CASE WHEN due_at='' THEN 1 ELSE 0 END,due_at,CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END,created_at DESC,id LIMIT ? OFFSET ?",
                [*args, page_size, (page - 1) * page_size],
            ).fetchall()
            return {
                "items": [self._item(db, row, user) for row in rows],
                "total": total,
                "page": page,
                "page_size": page_size,
                "has_more": page * page_size < total,
                "timezone": timezone_name,
            }


def install(app, service):
    service.todos = Todos(service)

    @app.get("/api/todos")
    def listing(
        family_id: str = "",
        device_id: str = "",
        status: str = "all",
        period: str = "all",
        priority: str = "",
        assignee_user_id: str = "",
        list_name: str = "",
        q: str = "",
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
        timezone: str = "Asia/Shanghai",
        trashed: bool = False,
    ):
        return service.todos.list(
            viewer(),
            family_id=family_id,
            device_id=device_id,
            status=status,
            period=period,
            priority=priority,
            assignee_user_id=assignee_user_id,
            list_name=list_name,
            q=q,
            page=page,
            page_size=page_size,
            timezone_name=timezone,
            trashed=trashed,
        )

    @app.post("/api/todos", status_code=201)
    def create(body: CreateInput):
        return service.todos.create(viewer(), body.model_dump(exclude={"request_id"}), request_id=body.request_id)

    @app.get("/api/todos/{task_id}")
    def detail(task_id: str):
        return service.todos.get(viewer(), task_id)

    @app.patch("/api/todos/{task_id}")
    def update(task_id: str, body: UpdateInput):
        return service.todos.update(viewer(), task_id, body.revision, body.changes)

    @app.delete("/api/todos/{task_id}")
    def delete(task_id: str, revision: int = Query(..., ge=1)):
        return service.todos.delete(viewer(), task_id, revision)

    @app.post("/api/todos/{task_id}/restore")
    def restore(task_id: str, body: RevisionInput):
        return service.todos.restore(viewer(), task_id, body.revision)

    @app.post("/api/todos/{task_id}/purge")
    def purge(task_id: str, body: PurgeInput):
        return service.todos.purge(viewer(), task_id, body.revision, body.title)

    @app.post("/api/todo-delete-confirmations/{confirmation_id}/confirm")
    def confirm(confirmation_id: str):
        return service.todos.confirm_delete(viewer(), confirmation_id)

    @app.delete("/api/todo-delete-confirmations/{confirmation_id}")
    def cancel(confirmation_id: str):
        return service.todos.cancel_delete(viewer(), confirmation_id)

    @app.get("/api/todo-delete-confirmations/{confirmation_id}")
    def confirmation_status(confirmation_id: str):
        return service.todos.deletion_status(viewer(), confirmation_id)
