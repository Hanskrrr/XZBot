"""Read trajectory references under the caller's local-admin authentication."""
import hashlib
import re

from fastapi import APIRouter, HTTPException, Query


def attach_delivery(current, rows):
    """Join by exact account-local event and profile, never timestamp proximity.

    Older numeric QQ event IDs can be correlated without rewriting history.
    Older hashed event IDs cannot: report unknown rather than guess a delivery.
    Only fixed metadata leaves the delivery ledger, never chat text or tokens.
    """
    candidates = []
    for row in rows:
        key = row.pop("delivery_event_key", None)
        indirect=row["kind"] in {"summary","web_search","group_topic"} or row["status"]=="research_requested"
        row["delivery"] = {"state": "not_applicable" if indirect else "unknown"}
        if indirect:
            continue
        idle_id = row.pop('idle_attempt_id', None)
        if row['kind'] in {'group_idle','group_idle_manual'} and isinstance(idle_id,str) and re.fullmatch(r'[a-f0-9]{32}',idle_id):
            with current.ledger.db() as db:
                attempt = db.execute('SELECT status,error_code FROM group_idle_attempts WHERE id=? AND scope=? AND profile_id=?', (idle_id,row['scope'],current.profile_id)).fetchone()
            if attempt:
                status=attempt['status']
                row['delivery']={'state': 'sent' if status=='sent' else 'uncertain' if status=='uncertain' else 'pending' if status in {'queued','generating','prepared','authorized'} else 'not_sent','status':status,'mode':'text'}
                if attempt['error_code']:
                    row['delivery']['error_code'] = attempt['error_code']
                continue
        event = row.get("source_event_id")
        if key is None and isinstance(event,str) and re.fullmatch(r"-?[0-9]{1,20}",event):
            key = hashlib.sha256((row["scope"]+"|"+event).encode()).hexdigest()
        if isinstance(key,str) and re.fullmatch(r"[a-f0-9]{64}",key):
            candidates.append((row,key))
    if not candidates:
        return rows
    keys = list(dict.fromkeys(key for _,key in candidates))
    with current.ledger.db() as db:
        records = {record["event_key"]:record for record in db.execute(
            "SELECT event_key,reply_id,scope,user_id,profile_id,status,mode FROM deliveries WHERE event_key IN ("+
            ",".join("?" for _ in keys)+") AND profile_id=?", [*keys,current.profile_id])}
    for row,key in candidates:
        record = records.get(key)
        if record is None or record["scope"]!=row["scope"] or record["user_id"]!=row["user"]:
            continue
        status = record["status"]
        # A model call can succeed even when local policy later refuses send.
        state = {"sent":"sent", "partial":"partial", "partial_uncertain":"partial", "cancelled":"not_sent", "uncertain":"uncertain", "prepared":"unconfirmed",
                 "reserved":"pending", "processing":"pending", "demo":"simulated",
                 "context_changed":"not_sent"}.get(status,
                    "not_sent" if record["mode"]=="none" else "unknown")
        row["delivery"] = {"state":state,"status":status if re.fullmatch(r"[a-z_]{1,80}",status or "") else "unknown",
            "mode":record["mode"] if record["mode"] in {"text","voice","image","none"} else None,
            "reply_id":record["reply_id"]}
        from .segmented_delivery import metadata
        if segments := metadata(current.ledger, record["reply_id"]):
            row["delivery"]["segmented"] = segments
    return rows


def create_router(engine):
    router = APIRouter(prefix="/admin/trajectories")

    @router.get("")
    def listing(scope: str = Query(default="", max_length=512), user: str = Query(default="", max_length=160),
                kind: str = "", limit: int = Query(default=50, ge=1, le=100),
                before: int | None = Query(default=None, ge=1)):
        current = engine.current
        try:
            result = current.trajectories.list(scope=scope, user=user, kind=kind, limit=limit, before=before)
            attach_delivery(current,result["items"])
        except ValueError:
            raise HTTPException(400, "trajectory_filter_invalid") from None
        return dict(result, profile_id=current.profile_id)

    @router.get("/{number}")
    def detail(number: int):
        current = engine.current
        result = current.trajectories.get(number, current.content_root)
        if result is None:
            raise HTTPException(404, "trajectory_not_found")
        attach_delivery(current,[result])
        return dict(result, profile_id=current.profile_id)

    @router.delete("/{number}")
    def delete(number: int):
        current = engine.current
        if not current.trajectories.delete(number):
            raise HTTPException(404, "trajectory_not_found")
        return {"deleted": True, "profile_id": current.profile_id}

    return router
