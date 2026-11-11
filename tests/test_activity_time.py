"""Poll time, event intervals and prose recency are separate evidence."""
import json
import time
import pytest
from azusa_bot.context.activity_time import observe,describe,recent_claim
from azusa_bot import role_activity,activity_guard,conversation_config
from context_helpers import context
from test_role_activity import row,enable
from test_web_search import rig,event
from test_conversation_execution import Model


def poll(at,status,**changes):
    return row(at,'bilibili_live')|{'live_status':status,'session':'12345:900' if status==1 else ''}|changes


def sequence(start,*,ended=True,age=7200):
    previous=poll(start,1 if ended else 0)
    point=poll(start+60,0 if ended else 1)
    point['timeline']=observe(previous,point,start+60)
    for at in range(start+120,start+60+age+1,60):
        current=poll(at,point['live_status']);current['timeline']=observe(point,current,at);point=current
    return point


@pytest.mark.parametrize('ended',[False,True])
def test_repeated_polls_and_restart_do_not_make_old_transition_recent(ended):
    point=sequence(100000,ended=ended)
    # Serialize/reload exactly as a service restart does.
    timeline=json.loads(json.dumps(point))['timeline'];now=point['last_check']
    timing=describe(timeline,'offline' if ended else 'live',now)
    assert timing['transition']['earliest_at']==100000
    assert timing['transition']['latest_at']==100060
    assert timing['transition']['age_min_seconds']==7200
    assert timing['observed_for_seconds']==7200
    assert recent_claim(timing,started=not ended,now=now)=='too_old'


def test_initial_offline_is_not_just_ended_even_after_refresh():
    first=poll(1000,0);first['timeline']=observe({},first,1000)
    second=poll(1060,0);timeline=observe(first,second,1060)
    timing=describe(timeline,'offline',1060)
    assert timing['observed_for_seconds']==60 and timing['transition'] is None
    assert recent_claim(timing,started=False,now=1060)=='unobserved'


def test_errors_gaps_future_and_backward_observations_do_not_invent_transition():
    first=poll(1000,1);first['timeline']=observe({},first,1000)
    error=poll(1060,0,last_error='bilibili_http_412');error['timeline']=observe(first,error,1060)
    assert error['timeline']==first['timeline']
    recovered=poll(1100,0);timeline=observe(error,recovered,1100)
    assert timeline['transition']['earliest_at']==1000
    assert timeline['transition']['latest_at']==1100
    for point in [poll(1500,0),poll(1001,0,last_error='failed')]:
        output=observe(first,point,1500)
        assert output['transition'] is None
    assert observe(first,poll(999,0),1100)==first['timeline']
    assert observe(first,poll(1200,0),1100)==first['timeline']
    assert describe(timeline,'unknown',2000)['transition'] is None


def test_entire_interval_must_fit_and_time_is_rechecked_at_send():
    first=poll(1000,1);point=poll(1100,0)
    timing=describe(observe(first,point,1100),'offline',1850)
    assert timing['transition'] is None  # Stale state cannot justify a present claim.
    point['timeline']=observe(first,point,1100)
    for at in [1300,1500,1700,1850]:
        next_point=poll(at,0);next_point['timeline']=observe(point,next_point,at);point=next_point
    timing=describe(point['timeline'],'offline',1850)
    assert recent_claim(timing,started=False,now=1850)=='recent'
    assert recent_claim(timing,started=False,now=1950)=='uncertain'
    assert recent_claim(timing,started=False,now=2001)=='too_old'


def test_sync_persists_timing_and_config_window_without_reset(tmp_path):
    now=time.time();first=poll(now-120,1);enable(tmp_path,[first]);role_activity.sync(tmp_path,[first],now-120)
    last=poll(now-60,0);role_activity.sync(tmp_path,[last],now-60)
    role_activity.sync(tmp_path,[poll(now,0)],now)
    current=role_activity.current(tmp_path,now)['live'][0]
    assert current['checked_at']==now
    assert current['timing']['transition']['latest_at']==now-60
    cfg=role_activity.config(tmp_path)
    body={'enabled':True,'sources':cfg['sources'],'revision':role_activity.revision(cfg),'recent_transition_minutes':1}
    role_activity.save(tmp_path,body,[last])
    assert role_activity.current(tmp_path,now)['live'][0]['timing']['recency']=='uncertain'
    cfg=role_activity.config(tmp_path)
    role_activity.save(tmp_path,{'enabled':True,'sources':cfg['sources'],'revision':role_activity.revision(cfg)},[last])
    assert role_activity.config(tmp_path)['recent_transition_minutes']==1
    for invalid in [0,61,True,2.5]:
        with pytest.raises(ValueError):role_activity.save(tmp_path,body|{'recent_transition_minutes':invalid},[last])


def live_context(now,timing):
    return {'live':[{'source':'bilibili_live:12345','status':'offline','checked_at':now,'error':'','timing':timing}]}


@pytest.mark.parametrize('age,reason',[(60,''),(2000,'transition_too_old')])
def test_guard_rejects_time_claim_even_when_offline_state_matches(age,reason):
    now=100000;point=sequence(now-age-60,age=age)
    timing=describe(point['timeline'],'offline',now)
    result=activity_guard.check(None,{'text':'我刚下播，正瘫着呢。'},live_context(now,timing),now=now)
    assert result['reason']==reason
    assert result['status']==('conflict' if reason else 'consistent')
    assert result['temporal_claim']=='just_ended'
    assert activity_guard.check(None,{'text':'现在没在直播。'},live_context(now,timing),now=now)['status']=='consistent'


@pytest.mark.asyncio
@pytest.mark.parametrize('with_old_transition',[False,True])
async def test_unknown_or_old_end_gets_one_time_aware_repair(rig,with_old_transition):
    engine,settings,_,_=rig;now=time.time();first=poll(now-120,0);enable(engine.content_root,[first])
    if with_old_transition:
        # Seed the durable timing generated by continuous successful polling.
        point=sequence(int(now)-7200-60)
        state={'seen':[],'sources':{first['key']:point}}
        path=engine.content_root/'content/role_activity_state.json';path.write_text(json.dumps(state))
    role_activity.sync(engine.content_root,[poll(now,0)],now)
    model=Model([{'text':'刚下播，正瘫着呢。','mode':'voice'},{'text':'现在没在播，怎么啦？','mode':'text'}]);engine.provider_override=model
    reply=await engine.handle(event(text='在干嘛'))
    assert reply['text']=='现在没在播，怎么啦？' and len(model.calls)==2
    correction=context(model.calls[1])['activity_correction']
    assert correction['reason']==('transition_too_old' if with_old_transition else 'transition_unobserved')
    assert correction['expected']==correction['claimed']=='offline'
    assert json.loads(model.calls[1][-1]['content'].split('\n',1)[1])['current_message']['text']=='在干嘛'
    assert '刷新状态不会把旧下播变成刚下播' in model.calls[0][0]['content']
    rows=engine.trajectories.list()['items']
    assert rows[-1]['activity_check']['reason']==correction['reason']
