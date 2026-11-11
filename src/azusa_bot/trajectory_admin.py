"""Read trajectory references under the caller's local-admin authentication."""
import hashlib
import json
import math
import re

from fastapi import APIRouter, HTTPException, Query

from .participation_vote import numeric as valid_score
from .trajectories import MAX_TRAJECTORY_ID, REDACTED, public_exchange


EVENT_COLUMNS = ('event_key,reply_id,source_event_id,scope,user_id,status,mode,at,error_code,'
                 'queue_wait_seconds,trigger_kind,retry_after_seconds,window_entered,decision_path,decision_reason,'
                 'receipt_message_id,receipt_at,peer_bot')
MODEL_STAGES = {'group_plan': 'planning', 'group_topic': 'topic', 'reply': 'generation',
                'group_decision': 'generation', 'followup': 'generation'}
CALL_STAGES = MODEL_STAGES | {'web_search': 'tool', 'tts': 'synthesis'}
CALL_PURPOSES = {'group_plan': 'participation_plan', 'group_topic': 'topic_analysis', 'reply': 'generate_reply',
                 'group_decision': 'group_reply', 'followup': 'followup_reply', 'web_search': 'web_search',
                 'tts': 'speech_synthesis'}
EVENT_TRIGGERS = {'direct', 'group_observation', 'followup'}


def _event_calls(current, events, *, detail=False):
    """Match by event identity inside the active profile. Older traces can use their numeric source ID."""
    if not events:
        return {}
    by_key = {event['event_key']: event for event in events}
    legacy = {(event['scope'], event['user_id'], event['source_event_id']): event for event in events
              if re.fullmatch(r'-?[0-9]{1,20}', event['source_event_id'])}
    columns = ('*' if detail else
               'id,kind,trigger,scope,user_id,started_at,status,refs,result,preview,related_id,provider_called')
    with current.memory._lock:
        candidates = current.memory._db.execute(
            f'SELECT {columns} FROM trajectories WHERE started_at>=? ORDER BY id DESC LIMIT ?',
            (min(event['at'] for event in events) - 1, current.trajectories.MAX_RECORDS)).fetchall()
    associated = {}
    parents = {}
    for row in reversed(candidates):
        if row['kind'] not in CALL_STAGES or row['trigger'] not in EVENT_TRIGGERS:
            continue
        refs = json.loads(row['refs'])
        key = refs.get('message_event_key') or refs.get('delivery_event_key')
        if key:
            event = by_key.get(key)
        else:
            source = refs.get('source_event_id', '')
            if isinstance(source, str) and re.fullmatch(r'-?[0-9]{1,20}', source):
                event = legacy.get((row['scope'], row['user_id'], source))
            else:
                event = parents.get(row['related_id'])
        if (event is not None and (row['scope'], row['user_id']) == (event['scope'], event['user_id'])
                and row['started_at'] >= event['at'] - 1):
            parents[row['id']] = event
            associated.setdefault(event['event_key'], []).append(row)
    return associated


def _event_planning(calls):
    for call in reversed(calls):
        if call['kind'] != 'group_plan' or not call['provider_called']:
            continue
        result = json.loads(call['result'])
        score = result.get('confidence')
        threshold = json.loads(call['refs']).get('confidence_threshold')
        plan = None if json.loads(call['preview']).get('redacted') is True else result.get('participation_plan')
        action = plan.get('action') if isinstance(plan, dict) else None
        effective_action = result.get('effective_action') if action else None
        return {'trace_id': call['id'], 'status': call['status'],
                'score': score if valid_score(score) else None,
                'threshold': threshold if valid_score(threshold) else None,
                'action': action if action in ('reply', 'observe', 'wait') else None,
                'effective_action': effective_action if effective_action in ('reply', 'observe', 'wait') else None}
    return None


def _event_preview(calls):
    state = 'unavailable'
    for call in reversed(calls):
        if call['kind'] not in MODEL_STAGES:
            continue
        preview = public_exchange(json.loads(call['preview']))
        if preview['preview_redacted'] or preview['request_tail'] == REDACTED:
            state = 'redacted'
            break
        if preview['request_kind'] in ('message', 'mention'):
            if preview['request_kind'] == 'mention':
                state = 'mention_only'
            else:
                state = 'saved' if preview['request_tail'] else 'no_text'
            return {key: preview[key] for key in ('request_tail', 'request_kind', 'request_truncated')} | {
                'request_preview_state': state,
            }
    return {'request_tail': None, 'request_kind': None, 'request_truncated': False, 'request_preview_state': state}


def _call_purpose(call, parent):
    """Label the operation using its recorded stage and predecessor, including older traces."""
    kind = call['kind']
    if (call.get('context_budget') or {}).get('attempt') == 2:
        previous_error = parent.get('error_code')
        if previous_error == 'context_length_exceeded':
            return 'context_retry'
        if kind == 'group_plan' and previous_error in {'deepseek_output_not_json', 'deepseek_empty_content'}:
            return 'plan_output_retry'
        if kind == 'group_plan' and previous_error == 'conversation_plan_invalid':
            return 'plan_validation_retry'
        return 'model_retry'
    if kind in {'reply', 'group_decision', 'followup'}:
        if parent.get('status') == 'activity_conflict':
            return 'activity_rewrite'
        if parent.get('kind') == 'web_search':
            return 'web_search_reply'
        tools = (call.get('execution') or {}).get('tools') or []
        previous_tools = (parent.get('execution') or {}).get('tools') or []
        new_tool = tools[-1] if parent.get('status') == 'research_requested' and tools and tools[:-1] == previous_tools else None
        for tool in ('history_lookup', 'knowledge_lookup'):
            source = call.get(tool) or {}
            if new_tool == tool or (call['related_id'] and source.get('parent_trace_id') == call['related_id']):
                return tool + '_reply'
    return CALL_PURPOSES[kind]


def _parse_event_cursor(value):
    timestamp, _, reply_id = value.partition(':')
    try:
        at = float(timestamp)
    except ValueError:
        raise HTTPException(400, 'message_event_cursor_invalid') from None
    if not math.isfinite(at) or at < 0 or not re.fullmatch(r'[a-f0-9]{32}', reply_id):
        raise HTTPException(400, 'message_event_cursor_invalid')
    return at, reply_id


def _public_event(row, calls):
    from .delivery_errors import delivery_explanation

    event = {key: value for key, value in row.items() if key != 'event_key'}
    entered = row['window_entered']
    event['window_entered'] = bool(entered) if entered is not None else None
    event['peer_bot'] = bool(row['peer_bot'])
    event['model_call_count'] = sum(bool(call['provider_called']) and call['kind'] in MODEL_STAGES for call in calls)
    event['model_called'] = event['model_call_count'] > 0
    event['trajectory_ids'] = [call['id'] for call in calls[-6:]]
    event['explanation'] = delivery_explanation(row['error_code']) if row['error_code'] else None
    event['planning'] = _event_planning(calls)
    event.update(_event_preview(calls))
    return event


def _stop(event, calls):
    status = event['status']
    last = calls[-1] if calls else None
    if status in {'sent', 'prepared', 'authorized', 'cancelled', 'partial', 'partial_uncertain', 'uncertain', 'demo', 'context_changed'}:
        stage = 'delivery'
    elif status.startswith('queue'):
        stage = 'queue'
    elif status == 'heat_topic_not_ready':
        stage = 'topic'
    elif status in {'observation_excluded', 'activity_observed', 'memory_observed', 'group_chat_disabled'}:
        stage = 'window'
    elif last is None:
        stage = 'processing' if status in {'reserved', 'processing'} else 'local'
    elif last['stage'] == 'generation' and last['status'] == 'success' and status != 'processing':
        stage = 'post_generation'
    else:
        stage = last['stage']
    return {'stage': stage, 'reason': status,
            'error_code': event['error_code'] or (last['error_code'] if last else '')}


def attach_delivery(current, rows):
    """Join by exact account-local event and profile, never timestamp proximity.

    Older numeric QQ event IDs can be correlated without rewriting history.
    Older hashed event IDs cannot: report unknown rather than guess a delivery.
    Only fixed metadata leaves the delivery ledger, never chat text or tokens.
    """
    candidates = []
    for row in rows:
        key = row.pop("delivery_event_key", None)
        if row['status']=='activity_conflict':
            row['delivery']={'state':'not_sent','status':'activity_state_conflict','mode':row.get('mode')}
            continue
        indirect=row["kind"] in {"summary","web_search","group_topic","group_plan"} or row["status"]=="research_requested"
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
            "SELECT event_key,reply_id,scope,user_id,profile_id,status,mode,error_code,queue_wait_seconds,"
            "receipt_message_id,receipt_at FROM deliveries WHERE event_key IN ("+
            ",".join("?" for _ in keys)+") AND profile_id=?", [*keys,current.profile_id])}
    for row,key in candidates:
        record = records.get(key)
        if record is None or record["scope"]!=row["scope"] or record["user_id"]!=row["user"]:
            continue
        status = record["status"]
        # A model call can succeed even when local policy later refuses send.
        state = {"sent":"sent", "partial":"partial", "partial_uncertain":"partial", "cancelled":"not_sent", "uncertain":"uncertain", "prepared":"unconfirmed", "authorized":"unconfirmed",
                 "reserved":"pending", "queued":"pending", "processing":"pending", "demo":"simulated",
                 "context_changed":"not_sent"}.get(status,
                    "not_sent" if record["mode"]=="none" else "unknown")
        row["delivery"] = {"state":state,"status":status if re.fullmatch(r"[a-z_]{1,80}",status or "") else "unknown",
            "mode":record["mode"] if record["mode"] in {"text","voice","image","none"} else None,
            "reply_id":record["reply_id"]}
        if record['receipt_message_id']:
            row['delivery'].update(receipt_message_id=record['receipt_message_id'], receipt_at=record['receipt_at'])
        if record['queue_wait_seconds']>=0.1:
            row['delivery']['queue_wait_seconds']=record['queue_wait_seconds']
        if record['error_code']:
            from .delivery_errors import delivery_explanation
            row['delivery'].update(error_code=record['error_code'],explanation=delivery_explanation(record['error_code']))
        from .segmented_delivery import metadata
        if segments := metadata(current.ledger, record["reply_id"]):
            row["delivery"]["segmented"] = segments
    return rows


def create_router(engine):
    router = APIRouter(prefix="/admin/trajectories")

    @router.get('/events')
    def events(scope: str = Query(default='',max_length=512), user: str = Query(default='',max_length=160),
               limit: int = Query(default=40,ge=1,le=100),
               before: str | None = Query(default=None,max_length=96),
               message_id: str | None = Query(default=None,max_length=71)):
        current=engine.current
        clauses=['profile_id=?'];args=[current.profile_id]
        for key,value in (('scope',scope),('user_id',user)):
            if value:clauses.append(key+'=?');args.append(value)
        if before is not None:
            clauses.append('(at,reply_id)<(?,?)')
            args.extend(_parse_event_cursor(before))
        if message_id is not None:
            if not re.fullmatch(r'-?[0-9]{1,20}|sha256:[a-f0-9]{64}', message_id):
                raise HTTPException(400, 'message_id_filter_invalid')
            clauses.append("(source_event_id=? OR receipt_message_id=? OR EXISTS ("
                           "SELECT 1 FROM delivery_segments WHERE reply_id=deliveries.reply_id "
                           "AND status='sent' AND message_id=?))")
            args.extend((message_id, message_id, message_id))
        with current.ledger.db() as db:
            rows=[dict(r) for r in db.execute('SELECT '+EVENT_COLUMNS+' FROM deliveries WHERE '+
                ' AND '.join(clauses)+' ORDER BY at DESC,reply_id DESC LIMIT ?',args+[limit+1])]
        next_before = f"{rows[limit-1]['at']}:{rows[limit-1]['reply_id']}" if len(rows) > limit else None
        rows = rows[:limit]
        associated = _event_calls(current, rows)
        items = [_public_event(row, associated.get(row['event_key'], [])) for row in rows]
        return {'items':items,'profile_id':current.profile_id,'content_saved':False,
                'queue':current.group_queue.snapshot(scope,user),'next_before':next_before}

    @router.get('/events/{reply_id}')
    def event_detail(reply_id: str):
        current = engine.current
        if not re.fullmatch(r'[a-f0-9]{32}', reply_id):
            raise HTTPException(404, 'message_event_not_found')
        with current.ledger.db() as db:
            row = db.execute('SELECT '+EVENT_COLUMNS+' FROM deliveries WHERE reply_id=? AND profile_id=?',
                             (reply_id, current.profile_id)).fetchone()
        if row is None:
            raise HTTPException(404, 'message_event_not_found')
        row = dict(row)
        records = _event_calls(current, [row], detail=True).get(row['event_key'], [])
        calls = [current.trajectories._public(record) | {'stage': CALL_STAGES[record['kind']]} for record in records]
        by_id = {call['id']: call for call in calls}
        for call in calls:
            call['purpose'] = _call_purpose(call, by_id.get(call['related_id'], {}))
        attach_delivery(current, calls)
        event = _public_event(row, records)
        return {'event': event, 'calls': calls, 'model_call_count': event['model_call_count'],
                'stop': _stop(event, calls), 'profile_id': current.profile_id}

    @router.get("")
    def listing(scope: str = Query(default="", max_length=512), user: str = Query(default="", max_length=160),
                kind: str = "", limit: int = Query(default=50, ge=1, le=100),
                before: int | None = Query(default=None, ge=1),
                call_id: int | None = Query(default=None, ge=1, le=MAX_TRAJECTORY_ID)):
        current = engine.current
        try:
            result = current.trajectories.list(scope=scope, user=user, kind=kind, limit=limit,
                                               before=before, call_id=call_id)
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
