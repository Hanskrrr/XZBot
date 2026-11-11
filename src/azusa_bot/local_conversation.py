"""Bounded conversation evidence owned by one Engine, with no external I/O.

Native mentions and quotes remain facts. Addressees are a separate, defeasible
interpretation. A plan grants no network permission: the caller still owns
budgets, privacy, delivery, and the final check immediately before sending.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field
import math
import re
import threading
import time
import unicodedata
import uuid

from .group_addressing import is_addressed, vocative_target_groups


def _id(value):
    return isinstance(value, str) and 0 < len(value) <= 250 and not any(ord(c) < 32 for c in value)


def _label(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 60:
        return None
    value = value.strip()
    return value if any(c.isalnum() for c in value) and not any(unicodedata.category(c).startswith('C') for c in value) else None


def _continuation(text):
    # These are candidate cues, never a semantic classifier.
    return bool(re.search(r'[你您妳](?![们俩])', text[:600]) or text.strip() in
                {'继续', '继续吧', '然后呢', '还有呢', '再说说', '再来一次', '为什么', '为什么？', '为什么?'}
                or re.match(r'^\s*(?:(?:我)?补充(?:一下|一点|一句|说明)|刚才(?:我)?说错|(?:更正|纠正)一下|改成|等等[，,:：\s]*是)', text[:600]))


def _plural(text):
    return bool(re.match(r'^\s*(?:你们(?:两位|俩)|你俩)', text[:600]) or re.match(
        r'^\s*(?:大家|各位|你们|两位|二位)(?:[，,:：\s]|请|一起|都说说|都回答|怎么看|觉得)', text[:600]))


@dataclass
class _Thread:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    version: int = 0
    at: float = 0.


@dataclass
class _Room:
    self_id: str
    events: OrderedDict = field(default_factory=OrderedDict)
    aliases: OrderedDict = field(default_factory=OrderedDict)
    threads: OrderedDict = field(default_factory=OrderedDict)
    bindings: dict = field(default_factory=dict)
    plans: OrderedDict = field(default_factory=OrderedDict)
    order: int = 0


class LocalConversation:
    """An instance is private to its Engine, including on the same machine.

    ``observe`` accepts the current-message envelope (or a recent row), with
    role/is_bot and an optional ACK-proven ``response_to_message_id``. Repeated
    observation is idempotent. ``accept`` returns None on an invalid/stale model
    assessment. Public dictionaries are copies and cannot mutate the guards.
    """

    def __init__(self, *, clock=time.monotonic, max_scopes=64, max_events=128,
                 max_threads=64, ttl_seconds=900, followup_seconds=180):
        if any(type(n) is not int or not 1 <= n <= 4096 for n in (max_scopes, max_events, max_threads)):
            raise ValueError('local_conversation_capacity_invalid')
        if any(type(n) not in (int, float) or not math.isfinite(n) or n <= 0
               for n in (ttl_seconds, followup_seconds)):
            raise ValueError('local_conversation_duration_invalid')
        self.clock = clock
        self.max_scopes, self.max_events, self.max_threads = max_scopes, max_events, max_threads
        self.ttl_seconds, self.followup_seconds = ttl_seconds, min(followup_seconds, ttl_seconds)
        self._rooms = OrderedDict()
        self._lock = threading.RLock()

    def _prune(self, room, now):
        for number, node in list(room.events.items()):
            if now - node['at'] >= self.ttl_seconds:
                del room.events[number]
        while len(room.events) > self.max_events:
            room.events.popitem(last=False)
        for anchor, thread in list(room.threads.items()):
            if anchor not in room.events or now - thread.at >= self.ttl_seconds:
                del room.threads[anchor]
        while len(room.threads) > self.max_threads:
            room.threads.popitem(last=False)
        for user, binding in list(room.bindings.items()):
            if (now - binding['at'] >= self.followup_seconds or binding['thread_anchor'] not in room.threads
                    or any(number not in room.events for number in binding['evidence_message_ids'])):
                del room.bindings[user]
        for token, saved in list(room.plans.items()):
            if now - saved['at'] >= self.ttl_seconds or not self._saved_valid(room, saved):
                del room.plans[token]
        while len(room.plans) > self.max_events:
            room.plans.popitem(last=False)

    def _learn(self, room, user, name):
        name = _label(name)
        if not _id(user) or not name:
            return
        values = room.aliases.setdefault(user, [])
        canonical = unicodedata.normalize('NFKC', name).casefold()
        values[:] = [old for old in values if unicodedata.normalize('NFKC', old).casefold() != canonical]
        values.append(name)
        del values[:-4]
        room.aliases.move_to_end(user)
        while len(room.aliases) > self.max_events:
            room.aliases.popitem(last=False)

    def _node(self, room, source, now):
        if not isinstance(source, dict) or not _id(source.get('message_id')) or not _id(source.get('user_id')):
            raise ValueError('local_conversation_event_invalid')
        text = source.get('text', '')
        if not isinstance(text, str):
            raise ValueError('local_conversation_event_invalid')
        sender = source.get('sender') if isinstance(source.get('sender'), dict) else {}
        if source.get('role') != 'assistant':
            self._learn(room, source['user_id'], source.get('display_name') or sender.get('display_name'))
        available = source.get('mention_metadata_available')
        native_ids = source.get('mentioned_user_ids')
        if available is None:
            available = isinstance(native_ids, list) or isinstance(source.get('mentions'), list)
        mentions = []
        if available is True:
            values = native_ids if isinstance(native_ids, list) else [item.get('user_id') for item in source.get('mentions', [])[:20] if isinstance(item, dict)]
            mentions = list(dict.fromkeys(value for value in values[:20] if _id(value)))
            for member in source.get('mentions', [])[:20]:
                if isinstance(member, dict) and member.get('user_id') in mentions:
                    self._learn(room, member['user_id'], member.get('display_name'))
        quote = source.get('quoted_message')
        reply_to = source.get('reply_to_id') if _id(source.get('reply_to_id')) else None
        if not (isinstance(quote, dict) and quote.get('message_id') == reply_to and _id(quote.get('user_id'))):
            quote = None
        facts = dict(mentioned_user_ids=mentions, mention_metadata_available=available is True,
                     mentions_everyone=available is True and source.get('mentions_everyone') is True,
                     platform_mentioned=source.get('mentioned') is True,
                     platform_other_mentioned=source.get('other_mentioned') is True,
                     reply_to_id=reply_to, quoted_author_id=quote['user_id'] if quote else None,
                     quoted_author_source='native_quote' if quote else None,
                     quote_status='resolved' if quote else ('unavailable' if reply_to else 'none'))
        room.order += 1
        return dict(message_id=source['message_id'], user_id=source['user_id'], text=text[:6000],
                    role='assistant' if source.get('role') == 'assistant' and source['user_id'] == room.self_id else 'user',
                    is_bot=source.get('is_bot') is True, response_to_message_id=source.get('response_to_message_id'),
                    relation_active=source.get('relation_active') is not False,
                    facts=facts, at=now, order=room.order, target='unknown', addressee_ids=[],
                    source='unresolved', evidence_message_ids=[source['message_id']],
                    thread_anchor=source['message_id'], visible_thread_anchor=source['message_id'],
                    needs_assessment=True, suggested_action='wait',
                    seen_versions={}, visible_ids=frozenset(), plan_id=None)

    def _thread(self, room, anchor, now, *, advance=False):
        thread = room.threads.setdefault(anchor, _Thread(at=now))
        if advance:
            thread.version += 1
        thread.at = now
        room.threads.move_to_end(anchor)
        return thread

    def _bind(self, room, node):
        if node['target'] not in {'self', 'others'} or not node['addressee_ids']:
            return
        old = room.bindings.get(node['user_id'])
        if old and old['order'] > node['order']:
            return
        if old and set(old['addressee_ids']) != set(node['addressee_ids']):
            thread = room.threads.get(old['thread_anchor'])
            if thread:
                thread.version += 1
        room.bindings[node['user_id']] = {key:deepcopy(node[key]) for key in
            ('target', 'addressee_ids', 'thread_anchor', 'visible_thread_anchor', 'evidence_message_ids', 'at', 'order')}

    def _classify(self, room, node, names):
        facts, text = node['facts'], node['text']
        # Prioritize current public names and our configured aliases; no peer
        # files, inferred private aliases, or global actor directory are read.
        mapping = {room.self_id: [name for name in names[:32] if _label(name)]}
        for user, aliases in reversed(room.aliases.items()):
            if user == room.self_id:
                mapping[user] = list(dict.fromkeys(mapping[user] + aliases))
            elif len(mapping) < 16:
                mapping[user] = aliases
        groups = vocative_target_groups(text, mapping)
        vocatives = set().union(*groups) if groups else set()
        ambiguous_name = any(len(group) > 1 for group in groups)
        facts['self_name_candidate'] = (not ambiguous_name and any(
            is_addressed(text, [name]) for name in mapping[room.self_id]))
        native = facts['mentioned_user_ids']
        # A bare native call is unambiguous. Arbitrary prose mentioning an
        # account, even with a native @, still goes through semantic assessment.
        remainder = text.strip()
        for user in native:
            remainder = remainder.replace('@' + user, '').strip()
        bare_call = (bool(native) and not remainder.strip(' ,，:：!！?？、')
                     and not facts['reply_to_id'] and not facts['mentions_everyone'])
        reported = bool(re.match(r'^\s*(?:据说|听说|有人说|他说|她说|刚才|刚刚|之前|截图)', text[:600]))
        if bare_call:
            node.update(target='self' if room.self_id in native else 'others', addressee_ids=native,
                        source='native_call', needs_assessment=False)
        elif (native == [room.self_id] and not facts['reply_to_id'] and not facts['mentions_everyone']
              and not ambiguous_name and not (vocatives - {room.self_id})
              and not reported):
            # A sole native call normally requests a response. Known conflicting
            # recipients and obvious reported speech remain planner candidates.
            node.update(target='self', addressee_ids=[room.self_id], source='native_self_call', needs_assessment=False)
        elif (not facts['mention_metadata_available'] and facts['platform_mentioned']
              and not facts['platform_other_mentioned'] and not ambiguous_name and not reported
              and not (vocatives - {room.self_id})
              and not facts['reply_to_id']):
            # Older authenticated bridges know whether this account was called,
            # but supply no complete native ID list. Preserve that distinction.
            node.update(target='self', addressee_ids=[room.self_id], source='legacy_self_call', needs_assessment=False)
        elif groups and not ambiguous_name and not facts['reply_to_id'] and not (set(native) - vocatives):
            node.update(target='self' if room.self_id in vocatives else 'others', addressee_ids=sorted(vocatives),
                        source='public_vocative', needs_assessment=False)
        elif ambiguous_name:
            node['source'] = 'ambiguous_name'
        elif facts['mentions_everyone'] or _plural(text):
            node.update(target='group', source='group_candidate')
        elif (facts['quoted_author_id'] == room.self_id and not (set(native) - {room.self_id})
              and not facts['platform_other_mentioned'] and not (vocatives - {room.self_id})):
            node.update(target='self', addressee_ids=[room.self_id], source='quoted_self_candidate')
        elif not native and not facts['reply_to_id'] and _continuation(text):
            binding = room.bindings.get(node['user_id'])
            if binding:
                node.update(target=binding['target'], addressee_ids=list(binding['addressee_ids']),
                            source='local_continuation', thread_anchor=binding['thread_anchor'],
                            visible_thread_anchor=binding['visible_thread_anchor'],
                            evidence_message_ids=list(dict.fromkeys(binding['evidence_message_ids'] + [node['message_id']])),
                            needs_assessment=binding['target'] == 'self')
        node['suggested_action'] = ('reply' if node['target'] == 'self' else 'observe') if not node['needs_assessment'] else 'wait'

    def _view(self, scope, room, node):
        result = {key:deepcopy(node[key]) for key in ('message_id', 'user_id', 'facts', 'target', 'addressee_ids',
            'source', 'evidence_message_ids', 'thread_anchor', 'needs_assessment', 'suggested_action')}
        visible = node['visible_ids'].intersection(room.events)
        result['evidence_message_ids'] = [number for number in result['evidence_message_ids'] if number in visible]
        result['thread_anchor'] = node['visible_thread_anchor'] if node['visible_thread_anchor'] in visible else node['message_id']
        thread = room.threads.get(node['thread_anchor'])
        result.update(scope=scope, thread_id=thread.id if thread else None,
                      permitted_evidence_message_ids=[number for number in room.events if number in node['visible_ids']])
        return result

    def observe(self, scope, current_message, *, self_id, names=(), recent=()):
        if (not _id(scope) or not _id(self_id) or not isinstance(names, (list, tuple))
                or not isinstance(recent, (list, tuple))):
            raise ValueError('local_conversation_identity_invalid')
        with self._lock:
            now = self.clock()
            room = self._rooms.get(scope)
            if room is None or room.self_id != self_id:
                room = self._rooms[scope] = _Room(self_id)
            self._rooms.move_to_end(scope)
            while len(self._rooms) > self.max_scopes:
                self._rooms.popitem(last=False)
            self._prune(room, now)
            number = current_message.get('message_id') if isinstance(current_message, dict) else None
            if number in room.events:
                return self._view(scope, room, room.events[number])
            # Recent evidence can seed an Engine which has not observed these
            # rows yet. It never imports another instance's relation decisions.
            for row in recent[-self.max_events:]:
                if not isinstance(row, dict) or row.get('message_id') == number or row.get('message_id') in room.events:
                    continue
                age = row.get('age_seconds', 0)
                if type(age) not in (int, float) or not math.isfinite(age) or not 0 <= age < self.ttl_seconds:
                    continue
                try:
                    seed = self._node(room, row, now-age)
                except ValueError:
                    continue
                room.events[seed['message_id']] = seed
                self._thread(room, seed['thread_anchor'], now-age)
            node = self._node(room, current_message, now)
            room.events[node['message_id']] = node
            known_quote = room.events.get(node['facts']['reply_to_id'])
            if known_quote is not None and known_quote is not node and node['facts']['quoted_author_id'] is None:
                node['facts'].update(quoted_author_id=known_quote['user_id'], quote_status='resolved',
                                     quoted_author_source='local_observation')
            # A fetched quote is visible evidence, even outside the recent
            # window. Its old timestamp does not make it a new conversation turn.
            quote = current_message.get('quoted_message')
            if (node['facts']['quoted_author_source'] == 'native_quote'
                    and quote['message_id'] not in room.events):
                seed = self._node(room, quote, now)
                room.events[seed['message_id']] = seed
                self._thread(room, seed['thread_anchor'], now)
            self._classify(room, node, names)
            reference = node['response_to_message_id'] if node['role'] == 'assistant' else node['facts']['reply_to_id']
            referenced = room.events.get(reference) if _id(reference) else None
            if referenced and referenced is not node:
                node['thread_anchor'] = referenced['thread_anchor']
                node['visible_thread_anchor'] = reference
                node['evidence_message_ids'] = list(dict.fromkeys(
                    node['evidence_message_ids'] + [reference]))
            existing = node['thread_anchor'] in room.threads
            # A delayed receipt supplies public evidence, not a new human
            # intention. It must not revoke a question already waiting to plan.
            self._thread(room, node['thread_anchor'], now, advance=existing and node['role'] != 'assistant')
            if node['role'] == 'assistant':
                node.update(target='others', addressee_ids=[referenced['user_id']] if referenced else [],
                            source='confirmed_self_message', needs_assessment=False, suggested_action='observe')
                if referenced and referenced['target'] == 'self' and node['relation_active']:
                    # Only a reply to an already identified self-addressed
                    # request sustains that requester; unsolicited replies do not.
                    binding_node = dict(referenced, at=now, evidence_message_ids=[reference, node['message_id']])
                    self._bind(room, binding_node)
            elif not node['needs_assessment']:
                self._bind(room, node)
            node['seen_versions'] = {anchor:thread.version for anchor, thread in room.threads.items()}
            node['visible_ids'] = frozenset(room.events)
            self._prune(room, now)
            return self._view(scope, room, node)

    def get(self, scope, message_id):
        with self._lock:
            room = self._rooms.get(scope)
            if room is None:
                return None
            self._prune(room, self.clock())
            node = room.events.get(message_id)
            return self._view(scope, room, node) if node else None

    def begin_assessment(self, scope, message_id, message_ids=None):
        """Freeze actual visible evidence immediately before calling the planner.

        Queue waiting can deliver a real ACK after the triggering observation.
        Refresh visibility only; never refresh the versions that invalidate a
        candidate after a relevant new human turn or a recipient change. When
        supplied, message_ids are the actual prompt rows, not every retained
        local event: older relation state is not restored as textual evidence.
        """
        with self._lock:
            room = self._rooms.get(scope)
            if room is None:
                return None
            self._prune(room, self.clock())
            node = room.events.get(message_id)
            if node is None:
                return None
            thread = room.threads.get(node['thread_anchor'])
            if thread is None or node['seen_versions'].get(node['thread_anchor']) != thread.version:
                return None
            if message_ids is not None and (not isinstance(message_ids, (list, tuple, set, frozenset))
                    or len(message_ids) > self.max_events * 4 or any(not _id(number) for number in message_ids)):
                return None
            visible = frozenset(room.events if message_ids is None else
                                (number for number in message_ids if number in room.events))
            if message_id not in visible:
                return None
            node['visible_ids'] = visible
            return self._view(scope, room, node)

    def _self_evidence(self, room, node, evidence, response_to):
        for number in evidence:
            other = room.events[number]
            if number == node['message_id']:
                if (room.self_id in other['facts']['mentioned_user_ids'] or other['facts']['quoted_author_id'] == room.self_id
                        or not other['facts']['mention_metadata_available'] and other['facts']['platform_mentioned']
                        or other['facts'].get('self_name_candidate')):
                    return True
            elif other['user_id'] == node['user_id'] and other['target'] == 'self':
                return True
            elif other['role'] == 'assistant':
                if number == response_to:
                    # A different member can explicitly respond to our public
                    # answer. Merely citing any assistant row is insufficient.
                    return True
                question = room.events.get(other['response_to_message_id'])
                if (question and question['message_id'] in node['visible_ids']
                        and question['user_id'] == node['user_id'] and question['target'] == 'self'):
                    return True
        return False

    def accept(self, scope, message_id, assessment):
        plan, _ = self.accept_with_reason(scope, message_id, assessment)
        return plan

    def accept_with_reason(self, scope, message_id, assessment):
        """Return the plan or the first failed check under the same lock."""
        with self._lock:
            room = self._rooms.get(scope)
            if room is None:
                return None, 'observation_unavailable'
            if not isinstance(assessment, dict):
                return None, 'plan_not_object'
            now = self.clock()
            self._prune(room, now)
            node = room.events.get(message_id)
            if node is None or node['role'] == 'assistant':
                return None, 'observation_unavailable'
            action = assessment.get('action')
            ids, evidence = assessment.get('addressee_ids'), assessment.get('evidence_message_ids')
            response_to, anchor = assessment.get('response_to_message_id'), assessment.get('thread_anchor')
            reason = assessment.get('reason')
            if not isinstance(action, str) or action not in ('observe', 'wait', 'reply'):
                return None, 'action_invalid'
            if (not isinstance(ids, list) or len(ids) > 20
                    or any(not _id(value) for value in ids) or len(ids) != len(set(ids))):
                return None, 'addressees_invalid'
            if (not isinstance(evidence, list) or not 1 <= len(evidence) <= 20
                    or any(not _id(value) for value in evidence) or len(evidence) != len(set(evidence))
                    or message_id not in evidence or any(value not in room.events or value not in node['visible_ids'] for value in evidence)):
                return None, 'evidence_invalid'
            if not _id(response_to) or response_to not in room.events or not _id(anchor) or anchor not in room.events:
                return None, 'anchors_invalid'
            if not isinstance(reason, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', reason):
                return None, 'reason_invalid'
            if response_to not in evidence or anchor not in node['visible_ids']:
                return None, 'anchors_invalid'
            known = {room.self_id}
            for number in node['visible_ids']:
                event = room.events.get(number)
                if event is None:
                    continue
                known.add(event['user_id'])
                known.update(event['facts']['mentioned_user_ids'])
                if event['facts']['quoted_author_id']:
                    known.add(event['facts']['quoted_author_id'])
            if assessment.get('target') == 'self' and room.self_id not in ids:
                return None, 'self_addressee_mismatch'
            if not set(ids) <= known:
                return None, 'addressee_unknown'
            target = 'self' if room.self_id in ids else 'others' if ids else assessment.get('target', 'unknown')
            if (not isinstance(target, str) or (not ids and target in {'self', 'others'}) or target not in {'self', 'others', 'group', 'unknown'}
                    or (ids and 'target' in assessment and assessment['target'] != target)):
                return None, 'target_addressee_mismatch'
            if not ids and target == 'unknown' and action == 'reply' and not _continuation(node['text']):
                target = 'group'
            if action == 'reply' and target not in {'self', 'group'}:
                return None, 'reply_target_invalid'
            if action == 'reply' and target == 'group' and _continuation(node['text']) and node['target'] != 'group':
                return None, 'ambiguous_continuation'
            if not node['needs_assessment'] and (target != node['target'] or set(ids) != set(node['addressee_ids'])):
                return None, 'local_target_conflict'
            if (action == 'reply' and target == 'self' and node['needs_assessment']
                    and not self._self_evidence(room, node, evidence, response_to)):
                return None, 'self_evidence_missing'
            # A planner cannot pull this event into an unrelated or future
            # thread merely by naming a real message. Its anchor must be cited.
            if anchor != message_id and anchor not in evidence:
                return None, 'anchors_invalid'
            # The durable local thread identity can predate the prompt's raw
            # history window. Preserve the model's visible anchor separately;
            # never make hidden old text a prerequisite or an invented citation.
            visible_anchor = anchor
            anchor = room.events[anchor]['thread_anchor']
            if response_to != message_id and room.events[response_to]['thread_anchor'] != anchor:
                return None, 'thread_anchor_mismatch'
            thread = room.threads.get(anchor)
            previous = room.threads.get(node['thread_anchor'])
            if thread is None or previous is None:
                return None, 'thread_unavailable'
            if (node['seen_versions'].get(anchor) != thread.version
                    or node['seen_versions'].get(node['thread_anchor']) != previous.version):
                return None, 'thread_version_changed'
            if anchor != node['thread_anchor']:
                thread.version += 1
                node['thread_anchor'] = anchor
            node.update(target=target, addressee_ids=list(ids), source='assessment' if node['needs_assessment'] else node['source'],
                        evidence_message_ids=list(evidence), visible_thread_anchor=visible_anchor,
                        needs_assessment=False, suggested_action=action)
            self._bind(room, node)
            node['seen_versions'][anchor] = thread.version
            plan = dict(scope=scope, message_id=message_id, user_id=node['user_id'], action=action, target=target,
                        addressee_ids=list(ids), response_to_message_id=response_to, evidence_message_ids=list(evidence),
                        thread_anchor=anchor, visible_thread_anchor=visible_anchor,
                        thread_id=thread.id, plan_id=uuid.uuid4().hex, reason=reason)
            room.plans[plan['plan_id']] = dict(plan=deepcopy(plan), at=now, version=thread.version)
            node['plan_id'] = plan['plan_id']
            self._prune(room, now)
            return deepcopy(plan), None

    def resolve_fast(self, scope, message_id):
        with self._lock:
            room = self._rooms.get(scope)
            if room is None:
                return None
            self._prune(room, self.clock())
            node = room.events.get(message_id)
            if node is None or node['needs_assessment'] or node['role'] == 'assistant':
                return None
            if node['plan_id']:
                saved = room.plans.get(node['plan_id'])
                return deepcopy(saved['plan']) if saved and self._saved_valid(room, saved) else None
            return self.accept(scope, message_id, dict(action=node['suggested_action'], target=node['target'],
                addressee_ids=node['addressee_ids'], response_to_message_id=message_id,
                evidence_message_ids=node['evidence_message_ids'], thread_anchor=node['visible_thread_anchor'], reason=node['source']))

    @staticmethod
    def _saved_valid(room, saved):
        plan = saved['plan']
        thread = room.threads.get(plan['thread_anchor'])
        return bool(thread and thread.id == plan['thread_id'] and thread.version == saved['version']
                    and plan['message_id'] in room.events and plan['response_to_message_id'] in room.events
                    and room.events[plan['message_id']]['plan_id'] == plan['plan_id']
                    and all(number in room.events for number in plan['evidence_message_ids']))

    def valid(self, plan):
        if not isinstance(plan, dict) or not _id(plan.get('scope')) or not _id(plan.get('plan_id')):
            return False
        with self._lock:
            room = self._rooms.get(plan.get('scope'))
            if room is None:
                return False
            self._prune(room, self.clock())
            saved = room.plans.get(plan.get('plan_id'))
            return bool(saved and all(plan.get(key) == value for key, value in saved['plan'].items())
                        and self._saved_valid(room, saved))

    def clear(self, scope=None, user=None):
        """Forget local evidence and invalidate guards; never recreate on read."""
        with self._lock:
            if user is None:
                if scope is None:
                    self._rooms.clear()
                else:
                    self._rooms.pop(scope, None)
                return
            for key, room in list(self._rooms.items()):
                if scope is not None and scope != key:
                    continue
                removed = {number for number, node in room.events.items()
                           if node['user_id'] == user or user in node['facts']['mentioned_user_ids']
                           or node['facts']['quoted_author_id'] == user or user in node['addressee_ids']}
                for number in removed:
                    del room.events[number]
                room.aliases.pop(user, None)
                room.bindings.pop(user, None)
                self._prune(room, self.clock())
