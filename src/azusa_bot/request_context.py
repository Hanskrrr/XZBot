"""Serialize bounded evidence before recent dialogue and the current QQ message.

Retrieval, trimming and image-source selection operate on their typed internal
layout. This pure boundary runs on every provider call, including tool rounds
and context retries. No history is fetched or persisted here.
"""
from copy import deepcopy
import json
import math

CHAT_KINDS = frozenset({'reply', 'group_decision', 'followup', 'group_idle'})
RECENT_KEYS = ('recent_group_messages', 'confirmed_direct_dialogue', 'confirmed_dialogue')
ROW_KEYS = ('role', 'user_id', 'message_id', 'display_name', 'text', 'mode', 'age_seconds')


def _data(message, prefix):
    if message.get('role') != 'user' or not isinstance(message.get('content'), str) or not message['content'].startswith(prefix):
        raise ValueError('request_context_shape_invalid')
    value = json.loads(message['content'][len(prefix):])
    if not isinstance(value, dict):
        raise ValueError('request_context_shape_invalid')
    return value


def _timeline(streams, anchor, *, direct_streams=()):
    """Merge overlapping ordered streams without comparing wall/monotonic clocks.

    Group observation order takes precedence over receipt-paired dialogue when
    concurrent replies interleave. Edges also retain same-second reply pairs;
    age breaks ties between unrelated streams. The current event
    is excluded by identity, never by matching its text.
    """
    rows, edges, incoming = {}, {}, {}

    def reaches(start, target):
        pending, seen = [start], set()
        while pending:
            node = pending.pop()
            if node == target:
                return True
            if node not in seen:
                seen.add(node)
                pending.extend(edges[node])
        return False

    for index, stream in enumerate(streams):
        previous = None
        for source in stream:
            if (source.get('role') not in {'user', 'assistant'}
                    or not isinstance(source.get('text'), str)
                    or not all(isinstance(source.get(k), str) for k in ('user_id', 'message_id'))):
                raise ValueError('request_context_row_invalid')
            key = (source['role'], source['user_id'], source['message_id'])
            if key == ('user', anchor.get('user_id'), anchor.get('message_id')):
                continue
            row = {k:deepcopy(source[k]) for k in ROW_KEYS if k in source}
            age = row.get('age_seconds', 0)
            if type(age) not in (int, float) or not math.isfinite(age) or age < 0:
                raise ValueError('request_context_age_invalid')
            if key not in rows:
                rows[key] = row; edges[key] = set(); incoming[key] = 0
            else:
                # Confirmed direct dialogue supplies mode; never refresh age.
                oldest = max(rows[key].get('age_seconds', 0), age)
                rows[key].update(row); rows[key]['age_seconds'] = oldest
            if index in direct_streams:
                rows[key]['in_direct_dialogue'] = True
            if (previous is not None and previous != key and key not in edges[previous]
                    and not reaches(key, previous)):
                # Earlier streams are authoritative. A receipt cache can group
                # A/reply-A before B/reply-B even when B arrived before reply-A.
                edges[previous].add(key); incoming[key] += 1
            previous = key
    order = {key:i for i,key in enumerate(rows)}
    ready = [key for key in rows if incoming[key] == 0]
    result = []
    while ready:
        key = min(ready, key=lambda k:(-rows[k].get('age_seconds',0), order[k]))
        ready.remove(key); result.append(rows[key])
        for following in edges[key]:
            incoming[following] -= 1
            if incoming[following] == 0:ready.append(following)
    if len(result) != len(rows):
        # Conflicting timelines must not be silently invented into one story.
        raise ValueError('request_context_order_conflict')
    return result


def render(messages, *, kind, prefixes, contract=None, contract_text=None):
    if kind not in CHAT_KINDS:
        return deepcopy(messages)
    prefix = prefixes['direct_header' if kind == 'reply' else 'group_header']
    data = _data(messages[1], prefix)
    anchor = deepcopy(data.get('current_message', {}))
    streams = [data.get(k, []) for k in RECENT_KEYS]
    if kind == 'followup':
        followup = _data(messages[2], prefixes['followup_header'])
        streams.append(followup.get('confirmed_dialogue', []))
        anchor['text'] = followup['latest_text']
        data['continuation_candidate'] = True
    elif kind == 'reply':
        # Explicit /voice or /text commands have already been stripped here.
        anchor['text'] = messages[-1]['content']
        # Legacy raw history is evidence, never new current conversation.
        if messages[2:-1]:data['earlier_history'] = deepcopy(messages[2:-1])
    if kind != 'group_idle' and not isinstance(anchor.get('text'), str):
        raise ValueError('request_context_anchor_missing')
    recent = _timeline(streams, anchor, direct_streams=(1, 2, 3))
    # All retrieval results and auxiliary metadata precede real dialogue.
    background = {k:v for k,v in data.items() if k not in (*RECENT_KEYS, 'current_message', 'addressed_candidate')}
    if 'addressed_candidate' in data:background['addressed_candidate'] = True
    result = [deepcopy(messages[0]), {'role':'user', 'content':prefix+json.dumps(background,ensure_ascii=False)}]
    if contract is not None:
        # Keep the effective control fields close to the dialogue even when
        # pinned background is long. This is application policy, never chat data.
        label = (prefixes['followup_header'] if kind == 'followup' else '') + contract_text
        result.append({'role':'system', 'content':label+'\n'+json.dumps(contract,ensure_ascii=False)})
    elif kind == 'followup':
        result[0]['content'] += '\n'+prefixes['followup_header'].rstrip()
    if recent or (kind == 'group_idle' and contract is not None):
        # An empty transcript truthfully represents a quiet room; keep a user
        # data envelope last without inventing a new message from a member.
        result.append({'role':'user', 'content':prefixes['recent_header']+json.dumps({'recent_conversation':recent},ensure_ascii=False)})
    if kind != 'group_idle':
        result.append({'role':'user', 'content':prefixes['current_header']+json.dumps({'current_message':anchor},ensure_ascii=False)})
    return result


def layout(messages, *, kind):
    """Content-free provenance for the admin trace; never save raw chat again."""
    if kind not in CHAT_KINDS:return None
    stages = ['persona', 'background']
    has_contract = len(messages)>2 and messages[2].get('role')=='system'
    if has_contract:stages.append('reply_contract')
    if len(messages) > (2 if kind == 'group_idle' else 3)+int(has_contract):stages.append('recent_conversation')
    if kind != 'group_idle':stages.append('current_message')
    return {'version':1, 'order':stages, 'current_message_last':kind != 'group_idle'}
