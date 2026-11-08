"""Read the exact local participation counters, without claiming or calling AI."""
from .group_idle import enabled, in_hours


def snapshot(engine,scope):
    settings=engine.settings(scope);now=engine.idle.clock();group=scope.rsplit(':',1)[-1]
    heat=engine.groups.heat(scope,settings)
    heat.update(required_messages=settings.group_heat_min_messages,required_members=settings.group_heat_min_members,
                progress=min(1.,heat['messages']/settings.group_heat_min_messages,heat['members']/settings.group_heat_min_members))
    with engine.ledger.db() as db:
        row=db.execute('SELECT last_at,version,attempted FROM group_idle_rooms WHERE scope=? AND profile_id=?',
                       (scope,engine.profile_id)).fetchone()
        count=db.execute("SELECT COUNT(*) FROM group_idle_attempts WHERE scope=? AND at>=? AND source='automatic'",
                         (scope,engine.idle._day_start())).fetchone()[0]
        # Reuse existing outcome metadata. No new transcript, prompt or paid
        # request is created by opening the monitor. The shared ledger must be
        # filtered by both this group and the currently selected Profile.
        recent=[dict(r) for r in db.execute(
            'SELECT reply_id,user_id,status,mode,at FROM deliveries '
            'WHERE scope=? AND profile_id=? ORDER BY at DESC LIMIT 20',
            (scope,engine.profile_id))]
    last=row['last_at'] if row else None
    elapsed=max(0.,now-last) if last is not None else None;threshold=settings.group_idle_minutes*60
    active=engine.is_active();configured=enabled(settings,group);allowed_hours=in_hours(settings,now)
    armed=bool(row and row['attempted']<row['version'])
    state=('bot_stopped' if not active else 'disabled' if not configured else
           'unobserved' if last is None else 'wait_new_message' if not armed else
           'outside_hours' if not allowed_hours else 'daily_limit' if count>=settings.group_idle_daily_limit else
           'waiting' if elapsed<threshold else 'time_reached')
    idle={'state':state,'last_activity_at':last,'elapsed_seconds':elapsed,'threshold_seconds':threshold,
          'progress':min(1.,elapsed/threshold) if elapsed is not None else None,
          'remaining_seconds':max(0.,threshold-elapsed) if elapsed is not None else None,
          'armed':armed,'attempts_today':count,'daily_limit':settings.group_idle_daily_limit,
          'hours_start':settings.group_idle_start_hour,'hours_end':settings.group_idle_end_hour}
    return {'scope':scope,'profile_id':engine.profile_id,'observed_at':now,'running':active,
            'participation_enabled':settings.group_chat_enabled,'heat':heat,'idle':idle,
            'confidence':engine.confidence_policy(scope,settings),'recent_events':recent}
