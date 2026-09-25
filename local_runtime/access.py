"""Local passwordless identities and server-enforced ownership, ready for real login."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar

from fastapi import HTTPException

CURRENT_USER = ContextVar("memory_current_user", default=None)
ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"
COOKIE = "knowin_identity"


def viewer():
    user = CURRENT_USER.get()
    if not user:
        raise HTTPException(401, "请先选择用户")
    return user


def valid_id(value):
    return bool(re.fullmatch(ID_PATTERN, value or ""))


def library_key(user, family="", device="", shared=False):
    identity = ["family" if shared else "personal", "" if shared else user, family, device]
    return "library_" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:32]


class Access:
    def __init__(self, path):
        self.path = path
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS app_users (
                  user_id TEXT PRIMARY KEY, name TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS app_families (
                  family_id TEXT PRIMARY KEY, name TEXT NOT NULL, owner_user_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS app_members (
                  family_id TEXT NOT NULL, user_id TEXT NOT NULL, PRIMARY KEY(family_id,user_id));
                CREATE TABLE IF NOT EXISTS app_devices (
                  device_id TEXT PRIMARY KEY, name TEXT NOT NULL, family_id TEXT NOT NULL DEFAULT '',
                  owner_user_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS app_logins (
                  token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS app_hidden (
                  user_id TEXT NOT NULL, memory_id TEXT NOT NULL, PRIMARY KEY(user_id,memory_id));
                CREATE TABLE IF NOT EXISTS app_deleted (
                  memory_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, deleted_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS app_migrations (name TEXT PRIMARY KEY);
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def bootstrap(self, items):
        """Import legacy bindings once. Never grant new memberships during a refresh."""
        with self.db() as db:
            if db.execute("SELECT 1 FROM app_migrations WHERE name='identities-v1'").fetchone():
                return
            scopes = list(db.execute("SELECT user_id,family_id,memory_user_id FROM chat_memory_scopes"))
            if any(row["user_id"] == "knowin_public" for row in scopes):
                db.execute("INSERT OR IGNORE INTO app_users VALUES ('knowin_public','公共资料（旧版只读）')")
            for item in items:
                meta = item.get("metadata") or {}
                raw = item.get("user_id", "")
                user = meta.get("owner_user_id") or (raw if not raw.startswith(("chat_personal_", "chat_family_", "library_")) else "")
                if valid_id(user) and user != "knowin_public":
                    scopes.append({"user_id": user, "family_id": meta.get("family_id", ""), "memory_user_id": raw})
            for row in scopes:
                user, family = row["user_id"], row["family_id"]
                if not valid_id(user) or user == "knowin_public":
                    continue
                db.execute("INSERT OR IGNORE INTO app_users VALUES (?,?)", (user, user))
                if valid_id(family):
                    db.execute("INSERT OR IGNORE INTO app_families VALUES (?,?,?)", (family, family, user))
                    db.execute("INSERT OR IGNORE INTO app_members VALUES (?,?)", (family, user))
            db.execute("INSERT OR IGNORE INTO app_users VALUES ('chat_default','默认用户')")
            db.execute("INSERT INTO app_migrations VALUES ('identities-v1')")

    def users(self):
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM app_users ORDER BY user_id")]

    def register(self, user, name):
        if not valid_id(user) or user == "knowin_public" or user.startswith(("chat_personal_", "chat_family_", "library_")):
            raise HTTPException(422, "用户 ID 使用 1–64 位字母、数字、点、下划线或短横线；此 ID 不可用")
        with self.db() as db:
            try:
                db.execute("INSERT INTO app_users VALUES (?,?)", (user, name.strip() or user))
            except sqlite3.IntegrityError:
                raise HTTPException(409, "用户已存在，请直接切换") from None

    def login(self, user, previous=""):
        with self.db() as db:
            if not db.execute("SELECT 1 FROM app_users WHERE user_id=?", (user,)).fetchone():
                raise HTTPException(404, "用户不存在")
            db.execute("DELETE FROM app_logins WHERE expires_at<? OR token_hash=?", (time.time(), self.hash(previous)))
            token = secrets.token_urlsafe(32)
            db.execute("INSERT INTO app_logins VALUES (?,?,?)", (self.hash(token), user, time.time() + 86400 * 7))
        return token

    @staticmethod
    def hash(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def authenticate(self, token):
        with self.db() as db:
            row = db.execute("SELECT user_id FROM app_logins WHERE token_hash=? AND expires_at>?",
                             (self.hash(token), time.time())).fetchone()
        return row[0] if row else None

    def profile(self, user):
        with self.db() as db:
            person = db.execute("SELECT * FROM app_users WHERE user_id=?", (user,)).fetchone()
            if not person:
                raise HTTPException(401, "请重新选择用户")
            families = [dict(row) for row in db.execute("""SELECT f.* FROM app_families f
                JOIN app_members m ON m.family_id=f.family_id WHERE m.user_id=? ORDER BY f.family_id""", (user,))]
            for family in families:
                family["members"] = [dict(row) for row in db.execute("""SELECT u.* FROM app_users u
                    JOIN app_members m ON m.user_id=u.user_id WHERE m.family_id=? ORDER BY u.user_id""", (family["family_id"],))]
            devices = [dict(row) for row in db.execute("""SELECT d.* FROM app_devices d WHERE
                (d.family_id='' AND d.owner_user_id=?) OR EXISTS
                (SELECT 1 FROM app_members m WHERE m.family_id=d.family_id AND m.user_id=?)
                ORDER BY d.device_id""", (user, user))]
        return {**dict(person), "families": families, "devices": devices, "mode": "passwordless_local", "read_only": user == "knowin_public"}

    def require_context(self, user, family="", device=""):
        profile = self.profile(user)
        if family and family not in {row["family_id"] for row in profile["families"]}:
            raise HTTPException(403, "当前用户不属于这个家庭")
        if device and not any(row["device_id"] == device and row["family_id"] == family for row in profile["devices"]):
            raise HTTPException(403, "当前家庭/用户下没有这台设备")
        return profile

    def create_family(self, user, family, name):
        if user == "knowin_public":
            raise HTTPException(403, "旧版公共资料身份只读，请切换到个人用户")
        if not valid_id(family):
            raise HTTPException(422, "家庭 ID 格式无效")
        with self.db() as db:
            try:
                db.execute("INSERT INTO app_families VALUES (?,?,?)", (family, name.strip() or family, user))
                db.execute("INSERT INTO app_members VALUES (?,?)", (family, user))
            except sqlite3.IntegrityError:
                raise HTTPException(409, "家庭已存在；请让家庭创建者添加成员") from None

    def add_member(self, user, family, member):
        with self.db() as db:
            row = db.execute("SELECT owner_user_id FROM app_families WHERE family_id=?", (family,)).fetchone()
            if not row or row[0] != user:
                raise HTTPException(403, "只有家庭创建者可以添加成员")
            if not db.execute("SELECT 1 FROM app_users WHERE user_id=?", (member,)).fetchone():
                raise HTTPException(404, "请先创建该用户")
            db.execute("INSERT OR IGNORE INTO app_members VALUES (?,?)", (family, member))

    def create_device(self, user, family, device, name):
        if user == "knowin_public":
            raise HTTPException(403, "旧版公共资料身份只读，请切换到个人用户")
        profile = self.require_context(user, family)
        if not valid_id(device):
            raise HTTPException(422, "设备 ID 格式无效")
        if family and not any(f["family_id"] == family and f["owner_user_id"] == user for f in profile["families"]):
            raise HTTPException(403, "只有家庭创建者可以添加家庭设备")
        with self.db() as db:
            try:
                db.execute("INSERT INTO app_devices VALUES (?,?,?,?)", (device, name.strip() or device, family, user))
            except sqlite3.IntegrityError:
                raise HTTPException(409, "设备 ID 已存在") from None

    def visible(self, items, user, include_hidden=False):
        from .chat import memory_key
        profile = self.profile(user)
        families = {f["family_id"]: f for f in profile["families"]}
        devices = {d["device_id"]: d for d in profile["devices"]}
        with self.db() as db:
            keys = {row[0] for row in db.execute("SELECT memory_user_id FROM chat_memory_scopes WHERE user_id=?", (user,))}
            hidden = {row[0] for row in db.execute("SELECT memory_id FROM app_hidden WHERE user_id=?", (user,))}
            deleted = {row[0] for row in db.execute("SELECT memory_id FROM app_deleted")}
        keys.update(memory_key(user, family) for family in ["", *families])
        keys.add(user)  # pre-identity CLI imports
        family_keys = {memory_key("", family, shared=True): family for family in families}
        visible = []
        for item in items:
            mid, raw, meta = item["id"], item["user_id"], item.get("metadata") or {}
            if mid in deleted or (mid in hidden and not include_hidden):
                continue
            family, device = meta.get("family_id", ""), meta.get("device_id", "")
            owner, scope = meta.get("owner_user_id", ""), ""
            if raw == "knowin_public":
                scope = "public"
            elif raw in family_keys:
                family, scope = family_keys[raw], "family"
            elif raw in keys:
                scope = "personal"
            elif meta.get("memory_type") == "builtin":
                expected = library_key(owner, family, device, meta.get("visibility") == "family")
                if raw == expected and ((meta.get("visibility") == "family" and family in families)
                                        or (meta.get("visibility") == "personal" and owner == user)):
                    scope = meta["visibility"]
            if not scope or (device and (device not in devices or devices[device]["family_id"] != family)):
                continue
            kind = "builtin" if raw == "knowin_public" or meta.get("source_file") or meta.get("memory_type") == "builtin" else "longterm"
            can_delete = scope == "personal" or (scope == "family" and (owner == user or families[family]["owner_user_id"] == user))
            visible.append({**item, "scope": scope, "family_id": family, "device_id": device,
                            "memory_type": kind, "can_delete": can_delete, "hidden": mid in hidden})
        return visible

    def hide(self, user, memory_id, hidden):
        with self.db() as db:
            if hidden:
                db.execute("INSERT OR IGNORE INTO app_hidden VALUES (?,?)", (user, memory_id))
            else:
                db.execute("DELETE FROM app_hidden WHERE user_id=? AND memory_id=?", (user, memory_id))
