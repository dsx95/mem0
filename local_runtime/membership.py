"""Atomic family departure and shared-task assignment cleanup."""

import json

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .access import viewer
from .todos import now


class LeaveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    successor_user_id: str = Field(default="", max_length=64)


def depart(service, chat, actor, family, member, successor=""):
    # Same lock order as existing management operations. No family conversation
    # can start between revocation and its authorization check.
    with chat.lock, service.memory_lock, service.access.db() as db:
        db.execute("BEGIN IMMEDIATE")
        owner = db.execute("SELECT owner_user_id FROM app_families WHERE family_id=?", (family,)).fetchone()
        if (
            not owner
            or not db.execute("SELECT 1 FROM app_members WHERE family_id=? AND user_id=?", (family, actor)).fetchone()
        ):
            raise HTTPException(403, "无权管理这个家庭")
        if actor != member and actor != owner[0]:
            raise HTTPException(403, "只有家庭管理员可以移除其他成员")
        if not db.execute("SELECT 1 FROM app_members WHERE family_id=? AND user_id=?", (family, member)).fetchone():
            raise HTTPException(404, "该用户已不在家庭中，请刷新")
        if any(chat.session(sid)["family_id"] == family for sid in chat.runs):
            raise HTTPException(409, "该家庭仍有对话正在回复，请完成或停止后再退出")
        if member == owner[0]:
            if (
                not successor
                or successor == member
                or not db.execute(
                    "SELECT 1 FROM app_members WHERE family_id=? AND user_id=?", (family, successor)
                ).fetchone()
            ):
                raise HTTPException(422, "家庭管理员退出前须指定一位现有成员接任；最后一位成员暂不能退出")
            db.execute("UPDATE app_families SET owner_user_id=? WHERE family_id=?", (successor, family))
        elif successor:
            raise HTTPException(422, "只有家庭管理员退出时需要指定接任者")
        stamp = now()
        rows = db.execute("SELECT * FROM todos WHERE family_id=? AND visibility='family'", (family,)).fetchall()
        unassigned, collaborations = 0, 0
        for row in rows:
            data = json.loads(row["data"])
            changes = {}
            if data["assignee_user_id"] == member:
                changes["assignee_user_id"] = {"before": member, "after": ""}
                data["assignee_user_id"] = ""
                unassigned += 1
            if member in data.get("participant_user_ids", []):
                before = data["participant_user_ids"]
                data["participant_user_ids"] = [uid for uid in before if uid != member]
                changes["participant_user_ids"] = {"before": before, "after": data["participant_user_ids"]}
                collaborations += 1
            if not changes:
                continue
            db.execute(
                "UPDATE todos SET assignee_user_id=?,data=?,updated_at=?,revision=revision+1 WHERE id=?",
                (data["assignee_user_id"], json.dumps(data, ensure_ascii=False), stamp, row["id"]),
            )
            service.todos.event(
                db,
                row["id"],
                row["revision"] + 1,
                actor,
                "member_left",
                changes,
            )
        # Old pending actions must not become valid again after a later rejoin.
        db.execute(
            "DELETE FROM todo_delete_confirmations WHERE user_id=? AND todo_id IN (SELECT id FROM todos WHERE family_id=?)",
            (member, family),
        )
        db.execute("DELETE FROM app_members WHERE family_id=? AND user_id=?", (family, member))
        db.execute(
            "INSERT INTO app_membership_events(family_id,actor,member,action,successor,created_at) VALUES (?,?,?,?,?,?)",
            (family, actor, member, "left" if actor == member else "removed", successor, stamp),
        )
        return {
            "left": True,
            "family_id": family,
            "member": member,
            "unassigned_todos": unassigned,
            "removed_collaborations": collaborations,
            "successor_user_id": successor,
        }


def install(app, service):
    with service.access.db() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS app_membership_events (
            id INTEGER PRIMARY KEY, family_id TEXT NOT NULL, actor TEXT NOT NULL,
            member TEXT NOT NULL, action TEXT NOT NULL, successor TEXT NOT NULL, created_at TEXT NOT NULL)""")

    @app.post("/api/identity/families/{family_id}/leave")
    def leave(family_id: str, body: LeaveInput):
        return depart(service, app.state.chat, viewer(), family_id, viewer(), body.successor_user_id)

    @app.delete("/api/identity/families/{family_id}/members/{member}")
    def remove(family_id: str, member: str):
        return depart(service, app.state.chat, viewer(), family_id, member)
