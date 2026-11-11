"""Temporal evidence from successful polls, never from an LLM's own prose.

A poll timestamp is not an event timestamp. A change is bounded by the last
observation of the old state and the first observation of the new state.
Repeated polls do not move that interval; gaps break continuity.
"""
from copy import deepcopy
import math

FRESH_SECONDS = 300
DEFAULT_RECENT_MINUTES = 15
STATES = {0: 'offline', 1: 'live', 2: 'replay'}


def timestamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _point(row, now):
    stamp = row.get('last_check')
    if (not row.get('enabled') or row.get('last_error') or not timestamp(stamp)
            or stamp > now or row.get('live_status') not in STATES):
        return None
    return {'status': STATES[row['live_status']], 'first_observed_at': stamp,
            'last_observed_at': stamp, 'session': row.get('session', ''), 'transition': None}


def observe(previous, row, now):
    old = previous.get('timeline') or _point(previous, now)
    point = _point(row, now)
    if point is None:
        return deepcopy(old) if old else None
    if not old:
        return point
    gap = point['last_observed_at'] - old['last_observed_at']
    if gap <= 0:
        return deepcopy(old)
    if gap > FRESH_SECONDS:
        return point
    if point['status'] != old['status']:
        point['transition'] = {'from': old['status'], 'to': point['status'],
                               'earliest_at': old['last_observed_at'],
                               'latest_at': point['last_observed_at']}
    elif point['status'] != 'live' or point['session'] == old['session']:
        point['first_observed_at'] = old['first_observed_at']
        point['transition'] = deepcopy(old.get('transition'))
    return point


def describe(timeline, status, now, *, recent_minutes=DEFAULT_RECENT_MINUTES):
    result = {'basis': 'successful_poll_interval', 'recent_window_seconds': recent_minutes * 60,
              'recency': 'unobserved', 'transition': None,
              'status_first_observed_at': None, 'observed_for_seconds': None}
    if (not timeline or status == 'unknown' or timeline.get('status') != status
            or not timestamp(timeline.get('last_observed_at'))
            or not 0 <= now-timeline['last_observed_at'] <= FRESH_SECONDS):
        return result
    first = timeline['first_observed_at']
    result.update(status_first_observed_at=first, observed_for_seconds=max(0, int(now-first)))
    transition = timeline.get('transition')
    if not transition:
        return result
    young = max(0, int(now-transition['latest_at']))
    old = max(0, int(now-transition['earliest_at']))
    result['transition'] = deepcopy(transition) | {'age_min_seconds': young, 'age_max_seconds': old}
    result['recency'] = ('recent' if old <= result['recent_window_seconds'] else
                         'too_old' if young > result['recent_window_seconds'] else 'uncertain')
    return result


def recent_claim(timing, *, started, now):
    """Only an observed, wholly recent interval supports 'just started/ended'."""
    timing = timing or {}
    transition = timing.get('transition') or {}
    relevant = (transition.get('to') == 'live' and transition.get('from') in {'offline', 'replay'}
                if started else transition.get('from') == 'live' and transition.get('to') == 'offline')
    if not relevant:return 'unobserved'
    earliest,latest=transition.get('earliest_at'),transition.get('latest_at')
    if not timestamp(earliest) or not timestamp(latest) or not earliest<=latest<=now:return 'unobserved'
    window=timing.get('recent_window_seconds',DEFAULT_RECENT_MINUTES*60)
    return 'recent' if now-earliest<=window else 'too_old' if now-latest>window else 'uncertain'
