"""Exact reply receipts link quotes to their original questions, never fuzzy recall.

Only IDs/hashes are added to storage. Original words stay in consented history
or the existing ephemeral dialogue. No QQ history scraping or image replay.
"""
import hashlib
import json
import re
import time
from .message_privacy import hidden_message
from .group_chat import GroupConversation
from .trajectories import fingerprint


def schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS quote_origins (
        scope TEXT NOT NULL, message_id TEXT NOT NULL,
        history_id INTEGER NOT NULL REFERENCES history(id) ON DELETE CASCADE,
        source_hash TEXT NOT NULL, parent_message_id TEXT,
        PRIMARY KEY(scope,message_id))''')
    db.execute('CREATE INDEX IF NOT EXISTS quote_origins_history ON quote_origins(history_id)')


def remember(engine,pending,message_id):
    scope,user=pending['scope'],pending['user_id']
    if not re.fullmatch(r'-?[0-9]{1,20}',message_id):return
    with engine.memory._lock,engine.memory._db:
        if not engine.memory._consent(scope,user) or engine.groups.opted_out(scope,user):return
        row=engine.memory._db.execute("SELECT id,text FROM history WHERE scope=? AND user_id=? AND event_id=? AND role='user'",
            (scope,user,pending['event_id'])).fetchone()
        if not row or hidden_message(row['text']):return
        engine.memory._db.execute('INSERT OR IGNORE INTO quote_origins VALUES (?,?,?,?,?)',
            (scope,message_id,row['id'],fingerprint(row['text']),pending.get('reply_to_id')))


def _allowed(engine,scope,user):
    return (not engine.groups.opted_out(scope,user)
            and engine.memory.collection_source(scope,user)!='member_disabled')


def _saved(engine,scope,message_id):
    with engine.memory._lock:
        row=engine.memory._db.execute('''SELECT h.*,q.source_hash,q.parent_message_id FROM quote_origins q
            JOIN history h ON h.id=q.history_id WHERE q.scope=? AND q.message_id=? AND h.scope=?''',
            (scope,message_id,scope)).fetchone()
        if not row:return None
        if (not engine.memory._consent(scope,row['user_id']) or not _allowed(engine,scope,row['user_id'])
                or fingerprint(row['text'])!=row['source_hash']
                or not 0<=time.time()-row['created_at']<=engine.memory.history_days*86400):return None
        return dict(row)


def _ephemeral(engine,scope,message_id):
    engine.dialogue.prune()
    for (room,user),turns in engine.dialogue.turns.items():
        if room!=scope or not _allowed(engine,scope,user):continue
        for question,answer in turns:
            if answer['message_id']==message_id:
                return dict(question,created_at=time.time()-max(0,engine.dialogue.clock()-question['at']),
                            event_id=question['message_id'],parent_message_id=None)
    return None


def _legacy(engine,scope,quote):
    if not quote.text.strip():return None
    matches={}
    # Full exact answer + native send timestamp + confirmed same-profile ledger.
    # Short repeated replies with more than one matching event are ambiguous.
    with engine.memory._lock:
        rows=engine.memory._db.execute("""SELECT user_id,refs,preview FROM trajectories
            WHERE scope=? AND kind IN ('reply','group_decision','followup') AND status='success'
              AND started_at BETWEEN ? AND ? ORDER BY id DESC LIMIT 101""",
            (scope,quote.sent_at-240,quote.sent_at+15)).fetchall()
        if len(rows)>100:return None  # Cannot claim uniqueness beyond the bounded window.
        for row in rows:
            preview=json.loads(row['preview']);refs=json.loads(row['refs'])
            if (preview.get('redacted') or preview.get('response_truncated')
                    or preview.get('response_text','').strip()!=quote.text.strip()):continue
            event=refs.get('source_event_id','')
            if not re.fullmatch(r'-?[0-9]{1,20}',event):continue
            key=hashlib.sha256((scope+'|'+event).encode()).hexdigest()
            with engine.ledger.db() as db:
                delivery=db.execute("SELECT status FROM deliveries WHERE event_key=? AND profile_id=?",(key,engine.profile_id)).fetchone()
            if not delivery or delivery['status']!='sent':continue
            user=row['user_id']
            if not _allowed(engine,scope,user) or not engine.memory._consent(scope,user):continue
            original=engine.memory._db.execute("SELECT * FROM history WHERE scope=? AND user_id=? AND role='user' AND event_id=?",(scope,user,event)).fetchone()
            if original and 0<=time.time()-original['created_at']<=engine.memory.history_days*86400:
                matches[original['id']]=dict(original,parent_message_id=None)
    return next(iter(matches.values())) if len(matches)==1 else None


def resolve(engine,event):
    quote=event.quoted_message
    if not event.reply_to_id or (quote and quote.user_id!=event.app_id):return None
    scope=f'{event.transport}:{event.app_id}:{event.kind}:{event.scope_id}'
    chain=[];seen=set();message_id=event.reply_to_id;method='receipt_link'
    for depth in range(3):
        if message_id in seen:break
        seen.add(message_id)
        row=_saved(engine,scope,message_id)
        if row is None and depth==0:
            row=_ephemeral(engine,scope,message_id);method='recent_dialogue'
            if row is None and quote:
                row=_legacy(engine,scope,quote);method='verified_legacy_answer'
        if row is None or hidden_message(row['text']) or GroupConversation.contains_credentials(row['text']):break
        # A DM can only resolve its own participant, never another contact.
        if event.kind=='dm' and row['user_id']!=event.user_id:break
        chain.append({'reply_message_id':message_id,'user_id':row['user_id'],
            'message_id':row['event_id'],'text':row['text'][:6000], 'truncated':len(row['text'])>6000,
            'sent_at':row['created_at'], **({'history_id':row['id']} if 'id' in row else {}),
            'source_hash':fingerprint(row['text'])})
        message_id=row.get('parent_message_id')
        if not message_id:break
    return {'status':'resolved' if chain else 'unavailable','method':method if chain else None,
            'original_questions':list(reversed(chain)), 'max_depth':3}


def references(origin):
    if not origin:return None
    return {k:origin[k] for k in ('status','method','max_depth')}|{'original_questions':[
        {k:row[k] for k in ('reply_message_id','user_id','message_id','sent_at','history_id','source_hash') if k in row}
        for row in origin['original_questions']]}


def detail(memory,scope,refs):
    if not refs:return None
    result=dict(refs,original_questions=[])
    with memory._lock:
        for ref in refs.get('original_questions',[]):
            item=dict(ref,available=False)
            row=memory._db.execute('SELECT text,created_at FROM history WHERE id=? AND scope=? AND user_id=?',
                (ref.get('history_id'),scope,ref['user_id'])).fetchone()
            if (row and memory._consent(scope,ref['user_id'])
                    and 0<=time.time()-row['created_at']<=memory.history_days*86400
                    and fingerprint(row['text'])==ref.get('source_hash') and not hidden_message(row['text'])
                    and not GroupConversation.contains_credentials(row['text'])):
                from .trajectories import safe_preview
                item.update(available=True,preview=safe_preview(row['text'],600))
            result['original_questions'].append(item)
    return result
