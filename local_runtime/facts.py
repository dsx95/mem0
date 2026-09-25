"""Authoritative, versioned facts and a durable, coalescing vector-index outbox.

The SQL transaction is the commit point. Qdrant is a derived index, never the
authority for current content or visibility. Designed for the single-process
local runtime; it does not turn Qdrant Local into a multi-worker server.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import threading
import time
import unicodedata
import uuid

from fastapi import HTTPException


def stamp():
    return datetime.now(timezone.utc).isoformat()


def normalized(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def effective_time(value):
    if not value:
        return stamp()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed > datetime.now(timezone.utc):
            raise ValueError
        return parsed.astimezone(timezone.utc).isoformat()
    except ValueError as exc:
        raise HTTPException(422, "发生时间须带时区且不能晚于当前时间；未来安排请写在事实内容中") from exc


class Facts:
    def __init__(self, service):
        self.service, self.access = service, service.access
        self.stop_event, self.wake = threading.Event(), threading.Event()
        self.worker = None
        self.sync_lock = threading.Lock()
        with self.access.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS facts (
                  id TEXT PRIMARY KEY, user_key TEXT NOT NULL, owner_user_id TEXT NOT NULL,
                  family_id TEXT NOT NULL, device_id TEXT NOT NULL, visibility TEXT NOT NULL,
                  subject TEXT NOT NULL DEFAULT '', attribute TEXT NOT NULL DEFAULT '',
                  subject_key TEXT NOT NULL DEFAULT '', attribute_key TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL, revision INTEGER NOT NULL, current_version INTEGER NOT NULL,
                  text TEXT NOT NULL, metadata TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS fact_slot ON facts(user_key,subject_key,attribute_key)
                  WHERE subject_key<>'' AND attribute_key<>'' AND status<>'deleted';
                CREATE TABLE IF NOT EXISTS fact_versions (
                  fact_id TEXT NOT NULL, version INTEGER NOT NULL, text TEXT NOT NULL,
                  status TEXT NOT NULL, kind TEXT NOT NULL, effective_at TEXT NOT NULL, valid_to TEXT,
                  recorded_at TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL,
                  source_turn_id TEXT NOT NULL, source_quote TEXT NOT NULL,
                  PRIMARY KEY(fact_id,version));
                CREATE TABLE IF NOT EXISTS fact_events (
                  id INTEGER PRIMARY KEY, fact_id TEXT NOT NULL, revision INTEGER NOT NULL,
                  action TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS fact_outbox (
                  fact_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, status TEXT NOT NULL,
                  attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                  error_type TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS fact_queue_due ON fact_outbox(status,next_attempt);
            """)

    def start(self):
        # The process owns the runtime lease; no previous worker is still alive.
        with self.access.db() as db:
            db.execute("UPDATE fact_outbox SET status='pending' WHERE status='running'")
        self.worker = threading.Thread(target=self._loop, daemon=True, name="fact-index")
        self.worker.start()

    def stop(self):
        self.stop_event.set()
        self.wake.set()
        if self.worker:
            self.worker.join()

    def bootstrap(self, items):
        """Import old long-term records without a model call or changed point ID.

        Repeated refreshes import new trusted CLI records, but never resurrect a
        deleted fact. Legacy text has no invented semantic subject/attribute.
        """
        with self.access.db() as db:
            db.execute("BEGIN IMMEDIATE")
            bindings = {r["memory_user_id"]: dict(r) for r in db.execute("SELECT * FROM chat_memory_scopes")}
            deleted = {r[0] for r in db.execute("SELECT memory_id FROM app_deleted")}
            known = {r[0] for r in db.execute("SELECT id FROM facts")}
            for item in items:
                meta = item.get("metadata") or {}
                if (
                    item["id"] in known
                    or item["id"] in deleted
                    or item["user_id"] == "knowin_public"
                    or meta.get("source_file")
                    or meta.get("memory_type") == "builtin"
                ):
                    continue
                binding = bindings.get(item["user_id"], {})
                owner = meta.get("owner_user_id") or binding.get("user_id") or item["user_id"]
                family = meta.get("family_id") or binding.get("family_id", "")
                created = item.get("created_at") or stamp()
                self._insert(
                    db,
                    item["id"],
                    item["user_id"],
                    owner,
                    family,
                    meta.get("device_id", ""),
                    meta.get("visibility", "personal"),
                    item["memory"],
                    "",
                    "",
                    meta,
                    created,
                )
                db.execute("UPDATE facts SET updated_at=? WHERE id=?", (item.get("updated_at") or created, item["id"]))
                self._version(
                    db,
                    item["id"],
                    1,
                    item["memory"],
                    "active",
                    "legacy",
                    created,
                    owner,
                    "旧版记录迁入；未推断实际发生时间",
                    "",
                    "",
                )
                db.execute("INSERT INTO fact_outbox VALUES (?,1,'done',0,0,'',?)", (item["id"], stamp()))

    @staticmethod
    def _insert(db, mid, key, owner, family, device, visibility, text, subject, attribute, metadata, created):
        db.execute(
            "INSERT INTO facts VALUES (?,?,?,?,?,?,?,?,?,?, 'active',1,1,?,?,?,?)",
            (
                mid,
                key,
                owner,
                family,
                device,
                visibility,
                subject,
                attribute,
                normalized(subject),
                normalized(attribute),
                text,
                json.dumps(metadata, ensure_ascii=False),
                created,
                created,
            ),
        )

    @staticmethod
    def _version(db, mid, version, text, status, kind, effective_at, actor, reason, source_turn_id, source_quote):
        db.execute(
            "INSERT INTO fact_versions VALUES (?,?,?,?,?,?,NULL,?,?,?,?,?)",
            (mid, version, text, status, kind, effective_at, stamp(), actor, reason, source_turn_id, source_quote),
        )

    @staticmethod
    def _event(db, mid, revision, action, actor):
        db.execute(
            "INSERT INTO fact_events(fact_id,revision,action,actor,created_at) VALUES (?,?,?,?,?)",
            (mid, revision, action, actor, stamp()),
        )
        db.execute(
            """INSERT INTO fact_outbox VALUES (?,?,'pending',0,0,'',?)
            ON CONFLICT(fact_id) DO UPDATE SET revision=excluded.revision,status='pending',attempts=0,
              next_attempt=0,error_type='',updated_at=excluded.updated_at""",
            (mid, revision, stamp()),
        )

    @staticmethod
    def _item(row, sync=None):
        meta = json.loads(row["metadata"])
        meta.update(
            owner_user_id=row["owner_user_id"],
            family_id=row["family_id"],
            device_id=row["device_id"],
            visibility=row["visibility"],
            memory_type="longterm",
        )
        return {
            "id": row["id"],
            "user_id": row["user_key"],
            "memory": row["text"],
            "category": "memory",
            "metadata": meta,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "fact_status": row["status"],
            "revision": row["revision"],
            "current_version": row["current_version"],
            "subject": row["subject"],
            "attribute": row["attribute"],
            "sync_status": sync or "pending",
        }

    def overlay(self, items, *, all_states=False):
        with self.access.db() as db:
            rows = db.execute(
                "SELECT f.*,o.status AS sync FROM facts f LEFT JOIN fact_outbox o ON f.id=o.fact_id"
            ).fetchall()
        owned = {r["id"] for r in rows}
        result = [i for i in items if i["id"] not in owned]
        result.extend(
            self._item(r, r["sync"])
            for r in rows
            if r["status"] != "deleted" and (all_states or r["status"] == "active")
        )
        return sorted(result, key=lambda i: i.get("updated_at") or i.get("created_at") or "", reverse=True)

    def get(self, user, mid, *, write=False):
        items = self.access.visible(self.overlay([], all_states=True), user, True)
        item = next((i for i in items if i["id"] == mid), None)
        if item is None:
            raise HTTPException(404, "事实不存在或不属于当前用户")
        if write and not item["can_delete"]:
            raise HTTPException(403, "仅创建者或家庭创建者可修改或删除；其他成员可对自己隐藏")
        with self.access.db() as db:
            versions = [
                dict(r) for r in db.execute("SELECT * FROM fact_versions WHERE fact_id=? ORDER BY version DESC", (mid,))
            ]
            events = [dict(r) for r in db.execute("SELECT * FROM fact_events WHERE fact_id=? ORDER BY id DESC", (mid,))]
        with self.service.memory_lock:
            legacy = self.service.memory.history(mid) if any(v["kind"] == "legacy" for v in versions) else []
        return {**item, "versions": versions, "events": events, "legacy_history": legacy}

    def submit(
        self,
        *,
        user,
        key,
        family="",
        device="",
        visibility="personal",
        text,
        subject="",
        attribute="",
        mid="",
        expected_revision=None,
        kind="assertion",
        occurred_at="",
        reason="",
        source_turn_id="",
        source_quote="",
        trusted=False,
    ):
        text, subject, attribute = text.strip(), subject.strip(), attribute.strip()
        # First-person subjects belong to the authenticated speaker, including
        # inside a shared family scope. Alice's "me" must never become Bob's.
        if normalized(subject) in {"本人", "我", "自己", "用户", "user", "me", "myself"}:
            subject = "user:" + user
        if (
            not text
            or len(text) > 6000
            or len(subject) > 120
            or len(attribute) > 120
            or bool(subject) != bool(attribute)
        ):
            raise HTTPException(422, "内容必填；对象和属性应同时填写，且长度不超过 120 字符")
        when = effective_time(occurred_at)
        self.access.require_context(user, family, device)
        if user == "knowin_public" or (visibility == "family" and not family):
            raise HTTPException(403, "当前身份或范围不允许写入事实")
        if kind not in {"assertion", "change", "correction"} or (not trusted and kind != "assertion"):
            raise HTTPException(422, "聊天只能提出事实或冲突候选，更新与纠错需用户确认")
        with self.service.memory_lock:
            if mid:
                # Members can flag a shared fact using its ID, but only a writer
                # can explicitly change/correct it. Untrusted assertions stay pending.
                item = self.get(user, mid, write=trusted)
                if item["user_id"] != key:
                    raise HTTPException(403, "不能修改当前保存范围以外的事实")
            with self.access.db() as db:
                db.execute("BEGIN IMMEDIATE")
                row = (
                    db.execute("SELECT * FROM facts WHERE id=? AND status<>'deleted'", (mid,)).fetchone()
                    if mid
                    else None
                )
                if mid and (row is None or expected_revision is None or row["revision"] != expected_revision):
                    raise HTTPException(409, "事实已变化，请刷新详情后重试")
                classified = False
                if (
                    row
                    and subject
                    and (normalized(subject), normalized(attribute)) != (row["subject_key"], row["attribute_key"])
                ):
                    if not trusted:
                        raise HTTPException(422, "对象和属性由用户管理，聊天不能重命名已有事实")
                    duplicate = db.execute(
                        "SELECT id FROM facts WHERE user_key=? AND subject_key=? AND attribute_key=? AND status<>'deleted' AND id<>?",
                        (key, normalized(subject), normalized(attribute), mid),
                    ).fetchone()
                    if duplicate:
                        raise HTTPException(409, "此对象和属性已有事实，请在已有记录中提交更新或冲突候选")
                    db.execute(
                        "UPDATE facts SET subject=?,attribute=?,subject_key=?,attribute_key=? WHERE id=?",
                        (subject, attribute, normalized(subject), normalized(attribute), mid),
                    )
                    classified = True
                if row is None and subject:
                    row = db.execute(
                        "SELECT * FROM facts WHERE user_key=? AND subject_key=? AND attribute_key=? AND status<>'deleted'",
                        (key, normalized(subject), normalized(attribute)),
                    ).fetchone()
                    if row and row["owner_user_id"] != user:
                        # A family member may flag conflict but cannot replace another person's fact.
                        trusted = False
                if row is None and not subject:
                    # Text replay is idempotent inside the exact visibility scope.
                    row = next(
                        (
                            r
                            for r in db.execute("SELECT * FROM facts WHERE user_key=? AND status<>'deleted'", (key,))
                            if r["text"] == text
                        ),
                        None,
                    )
                if row is not None:
                    mid = row["id"]
                    same = db.execute(
                        "SELECT version FROM fact_versions WHERE fact_id=? AND text=? AND status IN ('active','pending')",
                        (mid, text),
                    ).fetchone()
                    if same:
                        revision = row["revision"] + int(classified)
                        if classified:
                            db.execute("UPDATE facts SET revision=?,updated_at=? WHERE id=?", (revision, stamp(), mid))
                            self._event(db, mid, revision, "classified", user)
                        return {"id": mid, "revision": revision, "status": row["status"], "deduplicated": True}
                    revision = row["revision"] + 1
                    version = db.execute("SELECT MAX(version)+1 FROM fact_versions WHERE fact_id=?", (mid,)).fetchone()[
                        0
                    ]
                    current = db.execute(
                        "SELECT * FROM fact_versions WHERE fact_id=? AND version=?", (mid, row["current_version"])
                    ).fetchone()
                    late = when < current["effective_at"]
                    direct = (
                        trusted
                        and kind in {"change", "correction"}
                        and row["status"] != "disputed"
                        and (not late or kind == "correction")
                    )
                    self._version(
                        db,
                        mid,
                        version,
                        text,
                        "active" if direct else "pending",
                        kind,
                        when,
                        user,
                        reason,
                        source_turn_id,
                        source_quote,
                    )
                    if direct:
                        db.execute(
                            "UPDATE fact_versions SET status=?,valid_to=? WHERE fact_id=? AND version=? AND status='active'",
                            (
                                "retracted" if kind == "correction" else "superseded",
                                None if kind == "correction" else when,
                                mid,
                                row["current_version"],
                            ),
                        )
                        db.execute(
                            "UPDATE facts SET text=?,status='active',current_version=? WHERE id=?", (text, version, mid)
                        )
                    else:
                        db.execute("UPDATE facts SET status='disputed' WHERE id=?", (mid,))
                    db.execute("UPDATE facts SET revision=?,updated_at=? WHERE id=?", (revision, stamp(), mid))
                    self._event(db, mid, revision, kind if direct else "conflict_proposed", user)
                    status = "active" if direct else "disputed"
                else:
                    mid, revision, status = str(uuid.uuid4()), 1, "active"
                    meta = {"source": "chat" if source_turn_id else "manual", "source_turn_id": source_turn_id}
                    self._insert(
                        db, mid, key, user, family, device, visibility, text, subject, attribute, meta, stamp()
                    )
                    self._version(db, mid, 1, text, "active", kind, when, user, reason, source_turn_id, source_quote)
                    self._event(db, mid, 1, "created", user)
            self.wake.set()
        return {"id": mid, "revision": revision, "status": status, "deduplicated": False}

    def tasks(self, user):
        allowed = {i["id"]: i["can_delete"] for i in self.access.visible(self.overlay([], all_states=True), user, True)}
        with self.access.db() as db:
            deleted = {r[0] for r in db.execute("SELECT memory_id FROM app_deleted WHERE user_id=?", (user,))}
            return [
                {
                    **dict(r),
                    "can_retry": r["fact_id"] in deleted or allowed.get(r["fact_id"], False),
                    "operation": "删除索引及历史副本" if r["fact_id"] in deleted else "同步事实索引",
                }
                for r in db.execute("SELECT * FROM fact_outbox WHERE status<>'done' ORDER BY updated_at")
                if r["fact_id"] in allowed or r["fact_id"] in deleted
            ]

    def resolve(self, user, mid, revision, version, kind, reason):
        with self.service.memory_lock:
            self.get(user, mid, write=True)
            with self.access.db() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM facts WHERE id=?", (mid,)).fetchone()
                candidate = db.execute(
                    "SELECT * FROM fact_versions WHERE fact_id=? AND version=?", (mid, version)
                ).fetchone()
                current = db.execute(
                    "SELECT * FROM fact_versions WHERE fact_id=? AND version=?", (mid, row["current_version"])
                ).fetchone()
                if row["revision"] != revision or row["status"] != "disputed":
                    raise HTTPException(409, "冲突已变化，请刷新后再处理")
                if (
                    not candidate
                    or candidate["status"] not in {"active", "pending"}
                    or kind not in {"change", "correction"}
                ):
                    raise HTTPException(422, "请选择当前版本或待确认版本")
                if (
                    version != row["current_version"]
                    and candidate["effective_at"] < current["effective_at"]
                    and kind != "correction"
                ):
                    raise HTTPException(409, "迟到记录不能覆盖较新的事实；确认原记录错误时请选择纠错")
                if version != row["current_version"]:
                    db.execute(
                        "UPDATE fact_versions SET status=?,valid_to=? WHERE fact_id=? AND version=?",
                        (
                            "retracted" if kind == "correction" else "superseded",
                            None if kind == "correction" else candidate["effective_at"],
                            mid,
                            row["current_version"],
                        ),
                    )
                db.execute("UPDATE fact_versions SET status='rejected' WHERE fact_id=? AND status='pending'", (mid,))
                db.execute(
                    "UPDATE fact_versions SET status='active',reason=? WHERE fact_id=? AND version=?",
                    (reason, mid, version),
                )
                db.execute(
                    "UPDATE facts SET text=?,current_version=?,status='active',revision=revision+1,updated_at=? WHERE id=?",
                    (candidate["text"], version, stamp(), mid),
                )
                self._event(db, mid, revision + 1, "resolved_" + kind, user)
            self.wake.set()
        return self.get(user, mid)

    def retract(self, user, mid, revision):
        with self.service.memory_lock:
            self.get(user, mid, write=True)
            with self.access.db() as db:
                db.execute("BEGIN IMMEDIATE")
                count = db.execute(
                    "UPDATE facts SET status='retracted',revision=revision+1,updated_at=? WHERE id=? AND revision=?",
                    (stamp(), mid, revision),
                ).rowcount
                if not count:
                    raise HTTPException(409, "事实已变化，请刷新后重试")
                db.execute(
                    "UPDATE fact_versions SET status='retracted' WHERE fact_id=? AND status IN ('active','pending')",
                    (mid,),
                )
                self._event(db, mid, revision + 1, "retracted", user)
            self.wake.set()

    def erase(self, user, mid):
        """Caller has authorized deletion and holds memory_lock. Keep no fact text."""
        with self.access.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision,status FROM facts WHERE id=?", (mid,)).fetchone()
            if not row or row["status"] == "deleted":
                return
            db.execute(
                "UPDATE facts SET status='deleted',text='',subject='',attribute='',subject_key='',attribute_key='',metadata='{}',revision=revision+1,updated_at=? WHERE id=?",
                (stamp(), mid),
            )
            db.execute("DELETE FROM fact_versions WHERE fact_id=?", (mid,))
            db.execute("DELETE FROM fact_events WHERE fact_id=?", (mid,))
            self._event(db, mid, row["revision"] + 1, "deleted", user)
        self.wake.set()

    def sync_one(self, mid=None):
        # Exactly one bounded index operation per local runtime. Embedding never
        # holds the database lock; a revision check fences late model responses.
        if not self.sync_lock.acquire(blocking=False):
            return False
        try:
            return self._sync_one(mid)
        finally:
            self.sync_lock.release()

    def _sync_one(self, mid=None):
        with self.service.memory_lock:
            if getattr(self.service, "migration", None) and self.service.migration.pending:
                return False
            with self.access.db() as db:
                row = db.execute(
                    "SELECT * FROM fact_outbox WHERE "
                    + ("fact_id=? AND status='pending'" if mid else "status='pending' AND next_attempt<=?")
                    + " ORDER BY next_attempt LIMIT 1",
                    (mid or time.time(),),
                ).fetchone()
                if not row:
                    return False
                mid, revision = row["fact_id"], row["revision"]
                fact = db.execute("SELECT * FROM facts WHERE id=?", (mid,)).fetchone()
                db.execute("UPDATE fact_outbox SET status='running',updated_at=? WHERE fact_id=?", (stamp(), mid))
        try:
            memory = self.service.memory
            vector = memory.embedding_model.embed(fact["text"], "add") if fact["status"] == "active" else None
            with self.service.memory_lock:
                with self.access.db() as db:
                    latest = db.execute("SELECT revision FROM facts WHERE id=?", (mid,)).fetchone()
                if not latest or latest[0] != revision:
                    return True  # a newer SQL transaction owns the replacement task
                if getattr(self.service, "migration", None) and self.service.migration.pending:
                    with self.access.db() as db:
                        db.execute("UPDATE fact_outbox SET status='pending' WHERE fact_id=?", (mid,))
                    return False
                if fact["status"] != "active":
                    memory.vector_store.client.delete(self.service.settings.collection, [mid])
                    if fact["status"] == "deleted":
                        history = getattr(memory, "db", None)
                        if history is not None:
                            with history._lock, history.connection:
                                history.connection.execute("DELETE FROM history WHERE memory_id=?", (mid,))
                else:
                    item = self._item(fact)
                    payload = {
                        **item["metadata"],
                        "user_id": fact["user_key"],
                        "data": fact["text"],
                        "hash": hashlib.md5(fact["text"].encode()).hexdigest(),
                        "created_at": fact["created_at"],
                        "updated_at": fact["updated_at"],
                        "fact_revision": revision,
                        "fact_id": mid,
                    }
                    if hasattr(memory.vector_store, "insert"):
                        memory.vector_store.insert(vectors=[vector], ids=[mid], payloads=[payload])
                    else:
                        from qdrant_client.models import PointStruct

                        memory.vector_store.client.upsert(
                            self.service.settings.collection,
                            points=[PointStruct(id=mid, vector=vector, payload=payload)],
                        )
                with self.access.db() as db:
                    db.execute(
                        "UPDATE fact_outbox SET status='done',error_type='',updated_at=? WHERE fact_id=? AND revision=?",
                        (stamp(), mid, revision),
                    )
                self.service.refresh()
        except Exception as exc:
            attempts = row["attempts"] + 1
            with self.access.db() as db:
                db.execute(
                    "UPDATE fact_outbox SET status=?,attempts=?,next_attempt=?,error_type=?,updated_at=? WHERE fact_id=? AND revision=?",
                    (
                        "failed" if attempts >= 8 else "pending",
                        attempts,
                        time.time() + min(300, 2**attempts),
                        type(exc).__name__,
                        stamp(),
                        mid,
                        revision,
                    ),
                )
        return True

    def retry(self, user, mid):
        with self.access.db() as db:
            deleted = db.execute("SELECT 1 FROM app_deleted WHERE memory_id=? AND user_id=?", (mid, user)).fetchone()
        if not deleted:
            self.get(user, mid, write=True)
        with self.access.db() as db:
            db.execute(
                "UPDATE fact_outbox SET status='pending',attempts=0,next_attempt=0,error_type='' WHERE fact_id=? AND status IN ('pending','failed')",
                (mid,),
            )
        self.wake.set()

    def _loop(self):
        while not self.stop_event.is_set():
            # Bound each pass; pending rows remain in SQL, not an unbounded RAM queue.
            for _ in range(20):
                if self.stop_event.is_set() or not self.sync_one():
                    break
            self.wake.wait(2)
            self.wake.clear()
