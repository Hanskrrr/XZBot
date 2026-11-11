"""Account-wide, per-group rolling 24-hour synthesis reservations.

Reservations cover in-flight calls. Once synthesis starts, failure still counts
toward the paid-call limit. Profile switching and restarts do not reset usage.
"""
import time
import uuid


def voice_settings_error(settings, text, *, group):
    if group and settings.group_voice_mode == 'off':
        return 'group_voice_disabled'
    if not settings.voice_enabled:
        return 'voice_disabled'
    if not settings.voice_authorized:
        return 'voice_not_authorized'
    if len(text) > settings.max_voice_chars:
        return 'voice_text_too_long'
    return None


class GroupVoice:
    def __init__(self, ledger, clock=time.time):
        self.ledger, self.clock = ledger, clock
        with ledger.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS group_voice_usage (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL, at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS group_voice_scope ON group_voice_usage(scope,at);''')

    def status(self, scope, settings):
        now = self.clock()
        with self.ledger.db() as db:
            rows = db.execute('SELECT at FROM group_voice_usage WHERE scope=? AND at>? ORDER BY at',
                              (scope, now-86400)).fetchall()
        mode = settings.group_voice_mode
        limit = settings.group_voice_daily_limit if mode == 'limited' else None
        return {'mode':mode, 'used':len(rows), 'limit':limit,
                'remaining':max(0,limit-len(rows)) if limit is not None else None,
                'next_slot_at':rows[len(rows)-limit][0]+86400 if limit and len(rows)>=limit else None,
                'profile_voice_available':settings.voice_enabled and settings.voice_authorized}

    def reserve(self, scope, settings):
        if settings.group_voice_mode == 'off':
            return None
        now = self.clock()
        with self.ledger.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM group_voice_usage WHERE at<=?', (now-86400,))
            used = db.execute('SELECT COUNT(*) FROM group_voice_usage WHERE scope=?', (scope,)).fetchone()[0]
            if settings.group_voice_mode == 'limited' and used >= settings.group_voice_daily_limit:
                return None
            number = uuid.uuid4().hex
            db.execute('INSERT INTO group_voice_usage VALUES (?,?,?)', (number,scope,now))
            return number

    def release(self, number):
        with self.ledger.db() as db:
            db.execute('DELETE FROM group_voice_usage WHERE id=?', (number,))
