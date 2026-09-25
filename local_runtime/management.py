"""User-owned memory management and passwordless local identity endpoints."""
from __future__ import annotations

import json
import time
from typing import Literal

from fastapi import HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .access import COOKIE, library_key, viewer


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UserInput(Input):
    user_id: str = Field(min_length=1, max_length=64)
    name: str = Field(default="", max_length=80)


class FamilyInput(Input):
    family_id: str = Field(min_length=1, max_length=64)
    name: str = Field(default="", max_length=80)


class DeviceInput(Input):
    device_id: str = Field(min_length=1, max_length=64)
    family_id: str = Field(default="", max_length=64)
    name: str = Field(default="", max_length=80)


class NoteInput(Input):
    text: str = Field(min_length=1, max_length=6000)
    family_id: str = ""
    device_id: str = ""
    scope: Literal["personal", "family"] = "personal"
    memory_type: Literal["builtin", "longterm"] = "longterm"
    subject: str = Field(default="", max_length=120)
    attribute: str = Field(default="", max_length=120)
    occurred_at: str = Field(default="", max_length=40)


class FactUpdate(Input):
    revision: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=6000)
    kind: Literal["change", "correction", "assertion"] = "change"
    occurred_at: str = Field(default="", max_length=40)
    reason: str = Field(default="", max_length=1000)
    subject: str = Field(default="", max_length=120)
    attribute: str = Field(default="", max_length=120)


class FactResolution(Input):
    revision: int = Field(ge=1)
    version: int = Field(ge=1)
    kind: Literal["change", "correction"] = "change"
    reason: str = Field(min_length=1, max_length=1000)


class RevisionInput(Input):
    revision: int = Field(ge=1)


class HiddenInput(Input):
    hidden: bool


def filtered_context(access, user, family, device):
    profile = access.profile(user)
    if family not in {"", "__none__"} and family not in {f["family_id"] for f in profile["families"]}:
        raise HTTPException(403, "无权访问此家庭")
    if device not in {"", "__none__"} and not any(d["device_id"] == device for d in profile["devices"]):
        raise HTTPException(403, "无权访问此设备")


def matches(item, family, device):
    return ((not family or item.get("family_id", "") == ("" if family == "__none__" else family))
            and (not device or item.get("device_id", "") == ("" if device == "__none__" else device)))


def delete_memory(service, chat, user, memory_id):
    # Serialize deletion with reads/writes. A tombstone keeps interrupted deletes
    # out of retrieval even if an index or history cleanup must be retried.
    with chat.lock, service.memory_lock:
        if chat.runs:
            raise HTTPException(409, "仍有对话正在回复，请完成或停止回复后删除")
        item = next((i for i in service.access.visible(service.snapshot(all_states=True), user, True) if i["id"] == memory_id), None)
        with service.access.db() as db:
            pending = db.execute("SELECT user_id FROM app_deleted WHERE memory_id=?", (memory_id,)).fetchone()
        if not item and not (pending and pending[0] == user):
            raise HTTPException(404, "记录不存在或不属于当前用户")
        if item and not item["can_delete"]:
            raise HTTPException(403, "共享资料不能由当前用户删除，可以选择对我隐藏")
        if item and any(job["status"] in {"queued", "parsing", "writing"}
                        and job["user_id"] == item["user_id"]
                        and job["sha256"] == item["metadata"].get("source_sha256") for job in service.job_list()):
            raise HTTPException(409, "这份资料正在导入，请等待导入结束后删除")
        with service.access.db() as db:
            db.execute("INSERT OR IGNORE INTO app_deleted VALUES (?,?,?)", (memory_id, user, time.time()))
        service.facts.erase(user, memory_id)
        service.facts.sync_one(memory_id)
        if not (item and item.get("fact_status")) and any(i["id"] == memory_id for i in service.snapshot()):
            try:
                service.memory.delete(memory_id)
            except ValueError:
                if service.memory.vector_store.client.retrieve(service.settings.collection, [memory_id]):
                    raise
        history = getattr(service.memory, "db", None)
        if history is not None:
            with history._lock:
                with history.connection:
                    history.connection.execute("DELETE FROM history WHERE memory_id=?", (memory_id,))
        # Drop generated memory copies; original conversations remain explicitly
        # separate and can be removed via the conversation manager.
        with chat.db() as db:
            for row in db.execute("SELECT turn_id,user_id,family_id,diary_date,new_memories FROM daily_diary_entries").fetchall():
                records = json.loads(row["new_memories"])
                clean = [m for m in records if m.get("id") != memory_id]
                if clean != records:
                    db.execute("UPDATE daily_diary_entries SET new_memories=? WHERE turn_id=?", (json.dumps(clean), row["turn_id"]))
                    db.execute("""UPDATE daily_diaries SET summary='',new_preferences='[]',summary_status='pending',
                        source_version=source_version+1 WHERE user_id=? AND family_id=? AND diary_date=?""",
                               (row["user_id"], row["family_id"], row["diary_date"]))
            for row in db.execute("SELECT id,events,protocol,grounding FROM chat_turns").fetchall():
                if any(memory_id in row[key] for key in ("events", "protocol", "grounding")):
                    # Tool traces can contain both query results and remember text.
                    db.execute("UPDATE chat_turns SET events='[]',protocol='[]',grounding='{}' WHERE id=?", (row["id"],))
        service.refresh()
    return {"deleted": True, "id": memory_id, "conversations_preserved": True, "source_files_preserved": True}


def install(app, service):
    access, chat = service.access, app.state.chat

    @app.get("/api/identity/users")
    def users():
        return {"items": access.users(), "mode": "passwordless_local"}

    @app.post("/api/identity/users", status_code=201)
    def register(body: UserInput):
        access.register(body.user_id, body.name)
        return {"created": True, "user_id": body.user_id}

    @app.post("/api/identity/select")
    def select(body: UserInput, request: Request, response: Response):
        token = access.login(body.user_id, request.cookies.get(COOKIE, ""))
        response.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=request.url.scheme == "https", max_age=86400 * 7)
        return access.profile(body.user_id)

    @app.get("/api/identity/me")
    def me():
        return access.profile(viewer())

    @app.post("/api/identity/families", status_code=201)
    def family(body: FamilyInput):
        access.create_family(viewer(), body.family_id, body.name)
        return access.profile(viewer())

    @app.post("/api/identity/families/{family_id}/members")
    def member(family_id: str, body: UserInput):
        access.add_member(viewer(), family_id, body.user_id)
        return access.profile(viewer())

    @app.post("/api/identity/devices", status_code=201)
    def device(body: DeviceInput):
        access.create_device(viewer(), body.family_id, body.device_id, body.name)
        return access.profile(viewer())

    @app.get("/api/manage/memories")
    def memories(memory_type: Literal["all", "builtin", "longterm"] = "all", family_id: str = "", device_id: str = "",
                 q: str = "", include_hidden: bool = False, fact_status: Literal["all", "active", "disputed", "retracted"] = "all",
                 page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
        user = viewer()
        filtered_context(access, user, family_id, device_id)
        items = [i for i in access.visible(service.snapshot(all_states=True), user, include_hidden)
                 if matches(i, family_id, device_id) and (memory_type == "all" or i["memory_type"] == memory_type)
                 and (fact_status == "all" or i.get("fact_status", "active") == fact_status)
                 and (not q or q.casefold() in (i["memory"] + i["metadata"].get("source_file", "")).casefold())]
        return {"items": items[(page - 1) * page_size:page * page_size], "total": len(items), "page": page}

    @app.post("/api/manage/notes", status_code=201)
    def note(body: NoteInput):
        from .chat import memory_key
        user = viewer()
        if user == "knowin_public":
            raise HTTPException(403, "旧版公共资料身份只读，请切换到个人用户")
        access.require_context(user, body.family_id, body.device_id)
        if not body.text.strip() or (body.scope == "family" and not body.family_id):
            raise HTTPException(422, "内容不能为空，家庭共享必须选择家庭")
        key = (library_key(user, body.family_id, body.device_id, body.scope == "family") if body.memory_type == "builtin"
               else memory_key(user, body.family_id, shared=True) if body.scope == "family"
               else chat.memory_binding(user, body.family_id))
        if body.memory_type == "longterm":
            result = service.facts.submit(user=user, key=key, family=body.family_id, device=body.device_id,
                                          visibility=body.scope, text=body.text, subject=body.subject,
                                          attribute=body.attribute, occurred_at=body.occurred_at, trusted=True)
            return {**result, "results": [{"id": result["id"], "event": "CONFLICT" if result["status"] == "disputed" else "ADD"}]}
        with service.memory_lock:
            result = service.memory.add(body.text.strip(), user_id=key, infer=False, metadata={
                "owner_user_id": user, "family_id": body.family_id, "device_id": body.device_id,
                "visibility": body.scope, "memory_type": body.memory_type, "source": "manual"})
            service.refresh()
        return result

    @app.delete("/api/memories/{memory_id}")
    def remove_memory(memory_id: str):
        return delete_memory(service, chat, viewer(), memory_id)

    @app.post("/api/memories/{memory_id}/visibility")
    def hidden(memory_id: str, body: HiddenInput):
        if not any(i["id"] == memory_id for i in access.visible(service.snapshot(all_states=True), viewer(), True)):
            raise HTTPException(404, "记忆不存在")
        access.hide(viewer(), memory_id, body.hidden)
        return {"hidden": body.hidden}

    @app.get("/api/manage/facts/{memory_id}")
    def fact(memory_id: str):
        return service.facts.get(viewer(), memory_id)

    @app.get("/api/manage/tasks")
    def tasks():
        return {"items": service.facts.tasks(viewer())}

    @app.get("/api/manage/records/{memory_id}")
    def managed_detail(memory_id: str):
        item = next((i for i in access.visible(service.snapshot(all_states=True), viewer(), True) if i["id"] == memory_id), None)
        if item is None:
            raise HTTPException(404, "记录不存在")
        if item.get("fact_status"):
            return service.facts.get(viewer(), memory_id)
        with service.memory_lock:
            return {**item, "history": service.memory.history(memory_id)}

    @app.patch("/api/manage/facts/{memory_id}")
    def update_fact(memory_id: str, body: FactUpdate):
        item = service.facts.get(viewer(), memory_id, write=True)
        result = service.facts.submit(user=viewer(), key=item["user_id"], family=item["family_id"],
            device=item["device_id"], visibility=item["scope"], text=body.text, mid=memory_id,
            expected_revision=body.revision, kind=body.kind, occurred_at=body.occurred_at,
            reason=body.reason, subject=body.subject, attribute=body.attribute, trusted=True)
        return {**service.facts.get(viewer(), memory_id), "outcome": result["status"]}

    @app.post("/api/manage/facts/{memory_id}/resolve")
    def resolve_fact(memory_id: str, body: FactResolution):
        service.facts.resolve(viewer(), memory_id, body.revision, body.version, body.kind, body.reason)
        return service.facts.get(viewer(), memory_id)

    @app.post("/api/manage/facts/{memory_id}/retract")
    def retract_fact(memory_id: str, body: RevisionInput):
        service.facts.retract(viewer(), memory_id, body.revision)
        return {"retracted": True}

    @app.post("/api/manage/facts/{memory_id}/retry")
    def retry_fact(memory_id: str):
        service.facts.retry(viewer(), memory_id)
        return {"queued": True}

    @app.get("/api/manage/conversations")
    def conversations(family_id: str = "", device_id: str = "", q: str = "", page: int = Query(1, ge=1)):
        user = viewer()
        filtered_context(access, user, family_id, device_id)
        with chat.db() as db:
            items = [dict(row) for row in db.execute("""SELECT s.*,COUNT(t.id) AS turn_count
                FROM chat_sessions s LEFT JOIN chat_turns t ON t.session_id=s.id
                WHERE s.user_id=? GROUP BY s.id ORDER BY s.updated_at DESC""", (user,))]
        items = [i for i in items if matches(i, family_id, device_id) and (not q or q.casefold() in i["title"].casefold())]
        return {"items": items[(page - 1) * 20:page * 20], "total": len(items), "page": page}

    @app.get("/api/manage/diaries")
    def diaries(family_id: str = "", device_id: str = "", page: int = Query(1, ge=1)):
        user = viewer()
        filtered_context(access, user, family_id, device_id)
        with chat.db() as db:
            entries = [dict(row) for row in db.execute("""SELECT e.*,s.device_id FROM daily_diary_entries e
                JOIN chat_sessions s ON s.id=e.session_id WHERE e.user_id=? ORDER BY e.created_at DESC""", (user,))]
            summaries = {(r["family_id"], r["diary_date"]): dict(r) for r in db.execute("SELECT * FROM daily_diaries WHERE user_id=?", (user,))}
        grouped = {}
        for entry in entries:
            if not matches(entry, family_id, device_id):
                continue
            key = (entry["family_id"], entry["diary_date"])
            # A daily summary covers all devices, so never show it as a device-only summary.
            item = grouped.setdefault(key, {"date": entry["diary_date"], "family_id": entry["family_id"],
                "session_id": entry["session_id"], "turn_count": 0, "device_ids": [],
                "summary": "" if device_id else summaries.get(key, {}).get("summary", ""), "device_filtered": bool(device_id)})
            item["turn_count"] += 1
            if entry["device_id"] not in item["device_ids"]:
                item["device_ids"].append(entry["device_id"])
        items = sorted(grouped.values(), key=lambda i: (i["date"], i["family_id"]), reverse=True)
        return {"items": items[(page - 1) * 20:page * 20], "total": len(items), "page": page}
