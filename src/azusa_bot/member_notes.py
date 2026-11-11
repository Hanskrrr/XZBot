"""Versioned member introductions and dated interaction summaries.

Content is stored as ordinary scoped personal memories so consent, correction,
forgetting, provenance and backups retain one source of truth. This table adds
only lifecycle metadata. Call mutators inside the MemoryStore transaction.
"""
from datetime import datetime, timezone
import time
from .group_chat import GroupConversation
from .message_privacy import hidden_message

WINDOW_SECONDS = 72 * 3600
BACKGROUND_LIMIT = 700
INTERACTION_LIMIT = 400


def schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS member_context_notes (
        memory_id INTEGER PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
        note_type TEXT NOT NULL CHECK(note_type IN ('background','interaction')),
        period_start REAL, period_end REAL, version INTEGER NOT NULL DEFAULT 1
    )''')


def metadata(store, number):
    row=store._db.execute('SELECT * FROM member_context_notes WHERE memory_id=?',(number,)).fetchone()
    if not row:return {}
    data={k:row[k] for k in ('note_type','period_start','period_end','version')}
    for key in ('period_start','period_end'):
        if data[key] is not None:
            data[key+'_utc']=datetime.fromtimestamp(data[key],timezone.utc).isoformat()
    return data


def background(store, scope, user):
    with store._lock:
        row=store._db.execute('''SELECT m.* FROM memories m JOIN member_context_notes n ON n.memory_id=m.id
            WHERE m.scope=? AND m.subject=? AND n.note_type='background' ORDER BY m.id DESC LIMIT 1''',(scope,user)).fetchone()
        return store._memory_record(row) if row else None


def recent(store, scope, user, *, now=None, limit=12):
    now=time.time() if now is None else now
    with store._lock:
        rows=store._db.execute('''SELECT m.* FROM memories m JOIN member_context_notes n ON n.memory_id=m.id
            WHERE m.scope=? AND m.subject=? AND n.note_type='interaction'
              AND n.period_start>=? AND n.period_end<=?
            ORDER BY n.period_end DESC,m.id DESC LIMIT ?''',(scope,user,now-WINDOW_SECONDS,now,limit)).fetchall()
        return [store._memory_record(row) for row in rows]


def _text(text, limit):
    if (not isinstance(text,str) or not text.strip() or len(text)>limit
            or hidden_message(text) or GroupConversation.contains_credentials(text)):
        raise ValueError('member_note_invalid')
    return text.strip()


def _save(store,scope,user,text,kind,source,refs,*,existing=None,start=None,end=None):
    now=time.time()
    if existing:
        number=existing['id']
        store._db.execute('UPDATE memories SET text=?,source=?,updated_at=? WHERE id=?',(text,source,now,number))
        store._db.execute('DELETE FROM memory_sources WHERE memory_id=?',(number,))
        store._db.execute('UPDATE member_context_notes SET version=version+1 WHERE memory_id=?',(number,))
    else:
        number=store._db.execute("""INSERT INTO memories(scope,subject,actor,kind,text,source,created_at,updated_at)
            VALUES (?,?,?,'personal',?,?,?,?)""",(scope,user,user,text,source,now,now)).lastrowid
        store._db.execute('INSERT INTO member_context_notes VALUES (?,?,?,?,1)',(number,kind,start,end))
    store._db.executemany('INSERT OR IGNORE INTO memory_sources VALUES (?,?)',[(number,n) for n in refs])
    store._db.execute('INSERT OR REPLACE INTO memory_facets VALUES (?,?)',(number,'identity' if kind=='background' else 'experience'))
    store._audit('member_'+kind,scope,user,number)
    return number


def edit_background(store,scope,user,text,*,version=0):
    store._key(scope);store._key(user)
    text=_text(text,BACKGROUND_LIMIT)
    with store._lock,store._db:
        old=background(store,scope,user)
        if (old['version'] if old else 0)!=version:
            raise ValueError('member_background_changed')
        store._invalidate_summary(scope,user)
        store.trajectories.erase_previews(scope=scope)
        # This is now an explicit admin-authored introduction, not an automatic
        # inference. Like explicit memories it survives stopping collection.
        return _save(store,scope,user,text,'background','explicit',[],existing=old)


def apply_summary(store,scope,user,batch,refs,*,intro=None,interactions=None):
    """Validate everything before committing the existing summary transaction."""
    if interactions is not None and (not isinstance(interactions,list) or len(interactions)>6):
        raise ValueError('member_note_invalid')
    prepared=[]
    for item in ([('background',intro)] if intro is not None else [])+[
            ('interaction',v) for v in (interactions or [])]:
        kind,value=item
        if not isinstance(value,dict):raise ValueError('member_note_invalid')
        text=_text(value.get('text'),BACKGROUND_LIMIT if kind=='background' else INTERACTION_LIMIT)
        ids=value.get('source_ids')
        if (not isinstance(ids,list) or not ids or len(ids)>100
                or any(type(n) is not int or n not in refs for n in ids)):
            raise ValueError('member_note_invalid')
        if (not any(refs[n]['role']=='user' for n in ids)
                or kind=='background' and any(refs[n]['role']!='user' for n in ids)):
            raise ValueError('member_note_invalid')
        times=[refs[n]['created_at'] for n in ids]
        if kind=='interaction' and max(times)-min(times)>86400:
            raise ValueError('member_note_period_invalid')
        prepared.append((kind,text,sorted(set(ids)),min(times),max(times)))
    saved=[]
    for kind,text,ids,start,end in prepared:
        old=background(store,scope,user) if kind=='background' else None
        if kind=='background':
            # A manual introduction is pinned. An edit during generation wins.
            if old and old['source']=='explicit':continue
            expected=batch.get('previous_background')
            if (old or expected) and (not old or not expected or old['id']!=expected['id'] or old['version']!=expected['version']):
                continue
            if old:ids=sorted(set(ids+old['source_ids']))
        else:
            # Reprocessing retained history is idempotent. No duplicate recent
            # notes for the exact same evidence set, even with paraphrased text.
            existing=store._db.execute('''SELECT m.* FROM memories m JOIN member_context_notes n ON n.memory_id=m.id
                WHERE m.scope=? AND m.subject=? AND n.note_type='interaction' AND n.period_start=? AND n.period_end=?''',
                (scope,user,start,end)).fetchall()
            if any(store._memory_record(r)['source_ids']==ids for r in existing):continue
        saved.append(_save(store,scope,user,text,kind,'inferred',ids,existing=old,start=start,end=end))
    return saved


def view(store,scope,user):
    store._key(scope);store._key(user)
    with store._lock:
        return {'background':background(store,scope,user),'recent_interactions':recent(store,scope,user),
                'window_hours':72,'recording_enabled':store._consent(scope,user)}


def summary_dialogue(store,scope,user,statements):
    """Attach only this member's receipt-confirmed replies to claimed messages."""
    result=list(statements)
    with store._lock:
        for row in statements:
            replies=store._db.execute("""SELECT * FROM history WHERE scope=? AND user_id=? AND role='assistant'
                AND event_id IN (?,?) AND id>? AND created_at>=? ORDER BY id LIMIT 1""",
                (scope,user,row['event_id'],row['event_id']+':reply',row['id'],row['created_at'])).fetchall()
            result.extend(dict(r) for r in replies if not hidden_message(r['text']) and not GroupConversation.contains_credentials(r['text']))
    return sorted(result,key=lambda r:r['id'])
