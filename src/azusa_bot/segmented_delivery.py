"""At-most-once segment claims, per-segment receipts and partial dialogue commits.

The durable ledger stores delivery metadata only. Text plans remain ephemeral;
after restart an unfinished plan cannot be replayed or treated as delivered.
"""
import re
import time
from . import conversation_config


def schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS delivery_segments (
        reply_id TEXT NOT NULL, segment_index INTEGER NOT NULL, status TEXT NOT NULL,
        message_id TEXT, PRIMARY KEY(reply_id,segment_index))""")


def plan(engine, event, result, *, demo=False):
    if demo or event.transport != "onebot" or result.get("mode") != "text":
        return
    reply_id = result["reply_id"]
    pending = engine._group_pending.get(reply_id)
    # Commands and service messages remain atomic; only generated role speech.
    if not pending:
        return
    config = conversation_config.read(engine.content_root)["segmented_reply"]
    parts = conversation_config.split_text(result["text"], config)
    if len(parts) < 2:
        return
    pending["segments"] = parts
    pending["context_generation"] = engine.generation(pending["scope"])
    with engine.ledger.db() as db:
        db.executemany("INSERT INTO delivery_segments VALUES (?,?,'planned',NULL)",
                       [(reply_id, i) for i in range(len(parts))])
    result["segments"] = parts
    result["segment_interval_seconds"] = config["interval_seconds"]


def authorize(manager, reply_id, index):
    if type(index) is not int or not 0 <= index < 3:
        return False
    current = manager.current
    pending = current._group_pending.get(reply_id)
    if (not pending or "segments" not in pending or index >= len(pending["segments"])
            or pending["context_generation"] != current.generation(pending["scope"])
            or not manager.authorize_reply(reply_id)):
        return False
    with current.ledger.db() as db:
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("SELECT status FROM delivery_segments WHERE reply_id=? AND segment_index<?",
                           (reply_id, index)).fetchall()
        if len(prior) != index or any(row[0] != "sent" for row in prior):
            return False
        return bool(db.execute(
            "UPDATE delivery_segments SET status='authorized' WHERE reply_id=? AND segment_index=? AND status='planned' "
            "AND EXISTS (SELECT 1 FROM deliveries WHERE reply_id=delivery_segments.reply_id AND status='prepared')",
            (reply_id, index)).rowcount)


def acknowledge(manager, reply_id, index, status, message_id=None):
    if (type(index) is not int or not 0 <= index < 3 or status not in {"sent", "uncertain"}
            or status == "sent" and (not isinstance(message_id, str) or not re.fullmatch(r"-?[0-9]{1,20}", message_id))):
        raise ValueError("segment_ack_invalid")
    current = manager.current
    with current.ledger.db() as db:
        changed = db.execute("UPDATE delivery_segments SET status=?,message_id=? WHERE reply_id=? AND segment_index=? AND status='authorized'",
                             (status, message_id if status == "sent" else None, reply_id, index)).rowcount
    if changed:
        settle(current, reply_id, abort=status == "uncertain")
    return {"ok": True}


def settle(engine, reply_id, *, abort=False):
    with engine.ledger.db() as db:
        db.execute("BEGIN IMMEDIATE")
        if abort:
            db.execute("UPDATE delivery_segments SET status=CASE WHEN status='authorized' THEN 'uncertain' ELSE 'cancelled' END WHERE reply_id=? AND status IN ('planned','authorized')", (reply_id,))
        rows = [dict(row) for row in db.execute("SELECT * FROM delivery_segments WHERE reply_id=? ORDER BY segment_index", (reply_id,))]
        if not rows or any(row["status"] in {"planned", "authorized"} for row in rows):
            return
        delivery = db.execute("SELECT profile_id,profile_epoch,status FROM deliveries WHERE reply_id=?", (reply_id,)).fetchone()
        # Only the first finalizer may commit the aggregate receipt and history.
        if not delivery or delivery["status"] not in {"prepared", "context_changed"}:
            return
        sent = [row for row in rows if row["status"] == "sent"]
        uncertain = any(row["status"] == "uncertain" for row in rows)
        if len(sent) == len(rows):
            status = "sent"
        elif sent:
            status = "partial_uncertain" if uncertain else "partial"
        else:
            status = "uncertain" if uncertain else "cancelled"
        db.execute("UPDATE deliveries SET status=? WHERE reply_id=?", (status, reply_id))
    pending = engine._group_pending.get(reply_id)
    if (pending and "segments" in pending and engine.is_active()
            and delivery["profile_id"] == engine.profile_id and delivery["profile_epoch"] == engine.profile_epoch
            and pending["context_generation"] == engine.generation(pending["scope"])
            and time.monotonic() - pending["created_at"] < 240):
        if sent:
            parts = pending["segments"]
            pending["text"] = "\n".join(parts[row["segment_index"]] for row in sent)
            pending["confirmed_segments"] = [(row["message_id"], parts[row["segment_index"]]) for row in sent]
            engine._apply_reply_receipt(reply_id, "sent", sent[-1]["message_id"])
            return
    engine._apply_reply_receipt(reply_id, "uncertain")


def metadata(ledger, reply_id):
    with ledger.db() as db:
        rows = [dict(row) for row in db.execute("SELECT segment_index,status,message_id FROM delivery_segments WHERE reply_id=? ORDER BY segment_index", (reply_id,))]
    return {"planned": len(rows), "sent": sum(row["status"] == "sent" for row in rows), "segments": rows} if rows else None
