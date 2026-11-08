"""Global character profiles: isolated role data, shared transport and credentials.

The original profile stays in place. New profiles have independent content and
SQLite stores. A request holds an immutable runtime reference; changing the
global role invalidates old replies instead of mutating an in-flight engine.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import asyncio
import json
from pathlib import Path
import re
import shutil
import threading
import time
import uuid

from .settings import Settings, private_write, read_settings
from .bot_control import activity_allowed
from .group_addressing import address_names

VOICE_FIELDS = frozenset({"fish_reference_id", "fish_model", "voice_enabled", "voice_authorized"})
VOICE_STORED = VOICE_FIELDS | {"fish_model_override"}
DEFAULT_PERSONA = "# 新角色\n\n请在这里填写角色的名字、经历、性格、说话方式，以及与他人的关系。用角色自己的视角书写。\n"


class ProfileError(ValueError):
    pass


class ProfileStore:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / "data/profiles.json"
        self.lock = threading.RLock()
        with self.lock:
            if not self.path.exists():
                settings = read_settings(root)
                # Snapshot the *effective* legacy voice once. Reading only raw
                # Settings here could replace a working inherited DSH model
                # merely because the user later edits the profile's name.
                from .secrets import Credentials, load_credentials
                legacy = (load_credentials(settings.read_existing_dsh)
                          if not settings.fish_reference_id or not settings.fish_model_override else Credentials())
                voice={key:getattr(settings,key) for key in VOICE_STORED}
                voice["fish_reference_id"]=settings.fish_reference_id or legacy.fish_reference
                voice["fish_model"]=(settings.fish_model if settings.fish_model_override or not legacy.fish_model else legacy.fish_model)
                voice["fish_model_override"]=True
                now = time.time()
                self._write({"schema":1, "active_id":"default", "profiles":[{
                    "id":"default", "name":"默认角色", "description":"保留现有人设、资料与记忆。",
                    "created_at":now, "updated_at":now, "address_names":[],
                    "voice":voice}]})
            self._read()

    def _read(self):
        if self.path.is_symlink() or self.path.stat().st_size > 256 * 1024:
            raise ProfileError("profiles_manifest_invalid")
        try:
            data = json.loads(self.path.read_text())
            if data.get("schema") != 1 or not 1 <= len(data["profiles"]) <= 32:
                raise ValueError()
            ids = [self._id(p["id"]) for p in data["profiles"]]
            if len(set(ids)) != len(ids) or "default" not in ids or data["active_id"] not in ids:
                raise ValueError()
            for profile in data["profiles"]:
                profile["address_names"] = address_names(profile.get("address_names", []))
        except (KeyError, TypeError, ValueError):
            raise ProfileError("profiles_manifest_invalid") from None
        return data

    def _write(self, data):
        private_write(self.path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    @staticmethod
    def _id(value):
        if not isinstance(value,str) or not re.fullmatch(r"default|[a-f0-9]{32}",value):
            raise ProfileError("profile_id_invalid")
        return value

    @staticmethod
    def _text(value, field, maximum, *, required=False):
        if not isinstance(value,str) or len(value)>maximum or "\x00" in value or (required and not value.strip()):
            raise ProfileError("profile_" + field + "_invalid")
        return value.strip()

    def directory(self, profile_id):
        self._id(profile_id)
        if profile_id == "default":
            return self.root
        base = self.root / "data/profiles"
        path = base / profile_id
        if base.is_symlink() or path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ProfileError("profile_directory_invalid")
        return path

    def list(self):
        with self.lock:
            data = self._read()
            return {"active_id":data["active_id"], "profiles":[{
                **{k:v for k,v in p.items() if k != "voice"}, "active":p["id"] == data["active_id"],
                "voice":{k:v for k,v in p["voice"].items() if k in VOICE_FIELDS}}
                for p in data["profiles"]]}

    def get(self, profile_id):
        self._id(profile_id)
        with self.lock:
            item = next((p for p in self._read()["profiles"] if p["id"] == profile_id),None)
            if item is None:
                raise ProfileError("profile_not_found")
            return item

    def voice(self, profile_id):
        return self.get(profile_id)["voice"]

    @staticmethod
    def _voice(changes, old=None, *, strict_reference=True):
        if not isinstance(changes,dict) or set(changes)-VOICE_FIELDS:
            raise ProfileError("profile_voice_invalid")
        old = old or {"fish_model":"s2.1-pro", "fish_reference_id":"", "voice_enabled":False,
                      "voice_authorized":False, "fish_model_override":True}
        try:
            candidate = Settings.model_validate(old | changes)
        except ValueError:
            raise ProfileError("profile_voice_invalid") from None
        if candidate.voice_enabled and (not candidate.voice_authorized or (strict_reference and not candidate.fish_reference_id)):
            raise ProfileError("profile_voice_requires_reference_and_authorization")
        result = {k:getattr(candidate,k) for k in VOICE_STORED}
        if "fish_model" in changes:
            result["fish_model_override"] = True
        return result

    def create(self, body):
        if not isinstance(body,dict) or set(body)-{"name","description","persona","voice","address_names"}:
            raise ProfileError("profile_fields_invalid")
        name=self._text(body.get("name"),"name",60,required=True)
        description=self._text(body.get("description",""),"description",300)
        persona=body.get("persona",DEFAULT_PERSONA)
        if not isinstance(persona,str) or not 10 <= len(persona) <= 60000 or "\x00" in persona:
            raise ProfileError("persona_length_10_to_60000")
        voice=self._voice(body.get("voice",{}))
        names=self._address_names(body.get("address_names", []))
        with self.lock:
            data=self._read()
            if len(data["profiles"])>=32:
                raise ProfileError("profile_limit_reached")
            profile_id=uuid.uuid4().hex
            directory=self.directory(profile_id)
            directory.mkdir(parents=True,mode=0o700)
            now=time.time()
            item={"id":profile_id,"name":name,"description":description,"created_at":now,"updated_at":now,"voice":voice,"address_names":names}
            try:
                private_write(directory / "content/persona.md",persona)
                (directory / "content/lore").mkdir(mode=0o700)
                (directory / "content/assets").mkdir(mode=0o700)
                private_write(directory / "content/assets/manifest.json","{}\n")
                data["profiles"].append(item)
                self._write(data)
            except Exception:
                shutil.rmtree(directory)
                raise
            return self.public(item,data["active_id"])

    @staticmethod
    def _address_names(value):
        try:
            return address_names(value)
        except ValueError:
            raise ProfileError("profile_address_names_invalid") from None

    @staticmethod
    def public(item, active_id):
        return {**item,"address_names":item.get("address_names", []),"active":item["id"] == active_id,
                "voice":{k:v for k,v in item["voice"].items() if k in VOICE_FIELDS}}

    def update(self, profile_id, body):
        self._id(profile_id)
        if not isinstance(body,dict) or not body or set(body)-{"name","description","voice","address_names"}:
            raise ProfileError("profile_fields_invalid")
        with self.lock:
            data=self._read()
            item=next((p for p in data["profiles"] if p["id"]==profile_id),None)
            if item is None:
                raise ProfileError("profile_not_found")
            if "name" in body:
                item["name"]=self._text(body["name"],"name",60,required=True)
            if "description" in body:
                item["description"]=self._text(body["description"],"description",300)
            if "voice" in body:
                item["voice"]=self._voice(body["voice"],item["voice"],strict_reference=profile_id!="default")
            if "address_names" in body:
                item["address_names"]=self._address_names(body["address_names"])
            item["updated_at"]=time.time()
            self._write(data)
            return self.public(item,data["active_id"])

    def activate(self, profile_id):
        with self.lock:
            self.get(profile_id)
            data=self._read()
            data["active_id"]=profile_id
            self._write(data)

    def restore_voice(self, profile_id, voice):
        """Compensating write for a failed credential/settings transaction."""
        with self.lock:
            data=self._read()
            item=next((p for p in data["profiles"] if p["id"]==profile_id),None)
            if item is None:
                raise ProfileError("profile_not_found")
            item["voice"]=voice
            self._write(data)

    def delete(self, profile_id):
        with self.lock:
            self.get(profile_id)
            data=self._read()
            if profile_id == "default" or profile_id == data["active_id"]:
                raise ProfileError("profile_cannot_delete_default_or_active")
            directory=self.directory(profile_id)
            # Rename first so a failed manifest write can restore the full role.
            tombstone=directory.with_name("deleted-"+profile_id)
            directory.rename(tombstone)
            try:
                data["profiles"]=[p for p in data["profiles"] if p["id"]!=profile_id]
                self._write(data)
            except Exception:
                tombstone.rename(directory)
                raise
            shutil.rmtree(tombstone)
            return {"deleted":True,"backup_note":"已有备份保留独立副本；如需完全删除，请另行清理备份。"}


class ProfileEngineManager:
    """Compatibility facade; attributes resolve to the request's pinned engine."""
    def __init__(self, root):
        object.__setattr__(self,"root",root)
        object.__setattr__(self,"profiles",ProfileStore(root))
        object.__setattr__(self,"_pinned",ContextVar("xzbot_profile_engine",default=None))
        object.__setattr__(self,"_guard",threading.RLock())
        object.__setattr__(self,"_leases",{})
        object.__setattr__(self,"_retired",[])
        object.__setattr__(self,"_epoch",uuid.uuid4().hex)
        object.__setattr__(self,"_provider_override",None)
        object.__setattr__(self,"_subscription_handler",None)
        object.__setattr__(self,"_shared_capacity",asyncio.Semaphore(3))
        object.__setattr__(self,"_active",self._create_engine(self.profiles.list()["active_id"],self._epoch))

    def _create_engine(self, profile_id, epoch):
        from .core import Engine
        instance=Engine(self.root,profile_root=self.profiles.directory(profile_id),profile_id=profile_id,
            profile_epoch=epoch,voice_loader=lambda:self.profiles.voice(profile_id),
            active_guard=lambda:self._epoch == epoch)
        instance.provider_override=self._provider_override
        instance.subscription_handler=self._subscription_handler
        instance._capacity=self._shared_capacity
        instance.address_names_loader=lambda:self.profiles.get(profile_id)["address_names"]
        return instance

    @property
    def current(self):
        return self._pinned.get() or self._active

    @property
    def active_profile_id(self):
        return self._active.profile_id

    @contextmanager
    def pin(self):
        with self._guard:
            current=self._active
            self._leases[current]=self._leases.get(current,0)+1
            token=self._pinned.set(current)
        try:
            yield current
        finally:
            self._pinned.reset(token)
            with self._guard:
                self._leases[current]-=1
                if not self._leases[current]:
                    self._leases.pop(current)
                    if current in self._retired:
                        self._retired.remove(current)
                        current.memory.close()

    def __getattr__(self,name):
        return getattr(self.current,name)

    def __setattr__(self,name,value):
        if name=="provider_override":
            object.__setattr__(self,"_provider_override",value)
        if name=="subscription_handler":
            object.__setattr__(self,"_subscription_handler",value)
        setattr(self.current,name,value)

    def activate(self,profile_id):
        with self._guard:
            self.profiles.get(profile_id)
            if profile_id == self.active_profile_id:
                return self.profiles.list()
            epoch=uuid.uuid4().hex
            new=self._create_engine(profile_id,epoch)
            new._cloud_failure_count=self._active._cloud_failure_count
            new._cloud_retry_after=self._active._cloud_retry_after
            try:
                self.profiles.activate(profile_id)
            except Exception:
                new.memory.close()
                raise
            old=self._active
            object.__setattr__(self,"_epoch",epoch)
            object.__setattr__(self,"_active",new)
            old.invalidate_groups(reason="profile_changed")
            if self._leases.get(old):
                self._retired.append(old)
            else:
                old.memory.close()
            return self.profiles.list()

    def delete_profile(self,profile_id):
        with self._guard:
            if any(e.profile_id==profile_id for e in self._leases):
                raise ProfileError("profile_busy")
            return self.profiles.delete(profile_id)

    def authorize_reply(self,reply_id):
        if not activity_allowed(self.root):
            return False
        with self._guard, self._active.ledger.db() as db:
            row=db.execute("SELECT status,profile_id,profile_epoch,at,asset_reference FROM deliveries WHERE reply_id=?",(reply_id,)).fetchone()
            valid = bool(row and row["status"]=="prepared" and row["profile_id"]==self.active_profile_id
                        and row["profile_epoch"]==self._epoch and time.time()-row["at"]<240)
            if not valid:
                return False
            if row["asset_reference"] and row["asset_reference"] != "null":
                from .assets import still_approved
                try:
                    return still_approved(self._active.content_root,json.loads(row["asset_reference"]))
                except (ValueError,TypeError):
                    return False
            return True

    async def handle(self,event,*,demo=False):
        if self._pinned.get() is not None:
            return await self.current.handle(event,demo=demo)
        with self.pin() as current:
            return await current.handle(event,demo=demo)

    def invalidate_activity(self):
        """Cancel prepared replies and transient contexts for every live role."""
        with self._guard:
            for runtime in set([self._active, *self._retired, *self._leases]):
                runtime.invalidate_groups()

    def close(self):
        with self._guard:
            for runtime in [self._active,*self._retired]:
                runtime.memory.close()
