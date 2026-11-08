"""Bounded, in-memory, receipt-confirmed conversation turns.

Independent of long-term memory consent and unsolicited participation limits.
No historical database search or cross-member fallback is performed here.
"""
from collections import OrderedDict
import re


def requests_repeat(text):
    if re.search(r"(?:别|不要|不想|不用|不必).{0,4}(?:再|重)", text[:160]):
        return False
    return bool(re.search(r"(?:再|重新)(?:来|说|发|念|读|讲|唱|做|试|解释|回答|生成)(?:一[次遍个条首下]|一次|一遍|一下|下|次|遍)?|重来|重说|重发", text[:160]))


def refers_back(text):
    return len(text) <= 160 and (requests_repeat(text) or bool(re.search(
        r"继续|接着|刚才|刚刚|上一[个条句次轮]|换一?[个种条]|你说的|你发的|这[个句条].{0,8}(?:呢|什么意思|怎么|为啥)", text)))


class RecentDialogue:
    MAX_SESSIONS = 256
    MAX_TURNS = 6
    MAX_AGE = 7200

    def __init__(self, clock):
        self.clock = clock
        self.turns = OrderedDict()
        self.closed = set()

    def prune(self):
        cutoff = self.clock() - self.MAX_AGE
        for key, turns in list(self.turns.items()):
            kept = [turn for turn in turns if turn[0]['at'] > cutoff]
            if kept:
                self.turns[key] = kept
            else:
                self.turns.pop(key, None)
                self.closed.discard(key)

    def add(self, pending, message_id):
        self.prune()
        if pending.get('user_at') is None:
            return
        key = (pending['scope'], pending['user_id'])
        rows = self.turns.setdefault(key, [])
        if any(turn[0]['message_id'] == pending['event_id'] for turn in rows):
            return
        rows.append([
            {'role':'user', 'user_id':pending['user_id'], 'message_id':pending['event_id'],
             'text':pending['user_text'], 'at':pending['user_at']},
            {'role':'assistant', 'user_id':pending['self_id'], 'message_id':message_id,
             'text':pending['text'], 'mode':pending.get('mode', 'text'), 'at':self.clock()},
        ])
        del rows[:-self.MAX_TURNS]
        self.turns.move_to_end(key)
        self.closed.discard(key)
        while len(self.turns) > self.MAX_SESSIONS:
            evicted, _ = self.turns.popitem(last=False)
            self.closed.discard(evicted)

    def end_followup(self, scope, user):
        key = (scope, user)
        if key in self.turns:
            self.closed.add(key)

    def may_resume(self, scope, user):
        self.prune()
        return (scope, user) not in self.closed

    def get(self, scope, user, *, ttl_seconds, limit=12):
        self.prune()
        now = self.clock()
        turns = [turn for turn in self.turns.get((scope, user), [])
                 if 0 <= now-turn[0]['at'] < ttl_seconds]
        # Drop whole turns; never retain a detached old answer as current context.
        return [dict(row, age_seconds=max(0, int(now-row['at'])))
                for turn in turns[-max(1, limit//2):] for row in turn]

    def clear(self, scope=None):
        if scope is None:
            self.turns.clear()
            self.closed.clear()
        else:
            for key in list(self.turns):
                if key[0] == scope:
                    self.turns.pop(key, None)
                    self.closed.discard(key)
