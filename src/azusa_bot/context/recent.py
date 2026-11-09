"""Time-grounded recent events, derived only from retained source documents."""
from datetime import datetime, timedelta, timezone
import time

DAY = 86400
POST_LIMIT = 6
VIDEO_LIMIT = 24
CST = timezone(timedelta(hours=8))


def clock(now=None):
    now = time.time() if now is None else now
    return {'unix': now, 'utc': datetime.fromtimestamp(now, timezone.utc).isoformat(timespec='seconds'),
            'local': datetime.fromtimestamp(now, CST).isoformat(timespec='seconds'), 'timezone': 'Asia/Shanghai'}


def assessment(rows, current_time):
    """A freshness assessment, not an invented summary or model confidence."""
    now = current_time['unix']; events = []
    for row in rows:
        if row.get('selection_reason') != 'recent_activity': continue
        stamp = row.get('context_until', 0) - DAY
        if not 0 <= now - stamp < DAY: continue
        kind = row.get('activity_source', '').split(':', 1)[0]
        events.append({'doc_id': row['doc_id'], 'source': row['activity_source'],
                       'time_basis': 'observed_at' if kind == 'bilibili_live' else 'published_at',
                       'time': datetime.fromtimestamp(stamp, CST).isoformat(timespec='seconds'),
                       'age_seconds': int(now-stamp),
                       'content_event_time': 'state_observation' if kind == 'bilibili_live' else 'not_inferred'})
    return {'evaluated_at': current_time, 'window_hours': 24,
            'status': 'recent_records_available' if events else 'no_recent_records',
            'events': events, 'method': 'source_timestamp_window',
            'coverage': 'retrieved_records_only'}
