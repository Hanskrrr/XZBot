"""Fresh activity anchors and bounded repair of explicit contradictions."""
import json
import time
import pytest
from azusa_bot import activity_guard,role_activity,conversation_config
from azusa_bot.trajectory_admin import attach_delivery
from azusa_bot.settings import write_settings
from context_helpers import context
from test_web_search import rig,event
from test_conversation_execution import Model
from test_role_activity import row,enable
from test_group_idle import rig as idle_rig,ready,SELF,SCOPE


class IdleModel(Model):
    """The idle planner owns participation; generation only supplies prose."""
    def __init__(self, answers, *, action='reply'):
        super().__init__(answers)
        self.action=action

    async def complete(self, messages, **kwargs):
        if context(messages).get('task')=='group_plan':
            self.calls.append(messages)
            return {'confidence':.95,'participation_plan':{'action':self.action,'target':'group',
                'addressee_ids':[],'evidence_message_ids':[],'reason':'synthetic idle decision'}}
        return await super().complete(messages,**kwargs)


def live(now=None,status='live'):
    return {'live':[{'source':'bilibili_live:12345','status':status,'checked_at':time.time() if now is None else now,'error':''}]}


@pytest.mark.parametrize('text',['刚下播，正瘫着呢。','刚关的嘛，今晚播得短。','我现在没在直播。','我还没开播呢。'])
def test_explicit_denial_of_fresh_live_state(text):
    result=activity_guard.check(None,{'text':text},live())
    assert result['status']=='conflict' and result['claimed']=='offline'


@pytest.mark.parametrize('text',['阿梓刚下播，我还在播。','昨天刚下播就睡了。','你问“刚下播了吗”，我还播着呢。','我刚下播？明明在播呢。','如果刚下播就可以休息。','我没说自己刚下播。','刚下播的时候我还没吃饭。','没直播的那天出去吃饭了。','刚关了灯。','刚关的门又开了。'])
def test_does_not_guess_third_person_quotes_questions_or_past_events(text):
    result=activity_guard.check(None,{'text':text},live())
    assert result is None or result['status']!='conflict'


def test_freshness_offline_and_missing_state():
    assert activity_guard.check(None,{'text':'刚下播。'},live(time.time()-301)) is None
    assert activity_guard.check(None,{'text':'刚下播。'},None) is None
    assert activity_guard.check(None,{'text':'刚下播。'},live(status='unknown')) is None
    assert activity_guard.check(None,{'text':'我正在直播。'},live(status='offline'))['status']=='conflict'
    assert activity_guard.check(None,{'text':'刚下播就走了。'},live(),request_text='昨天去哪了') is None


def bind(engine):
    now=time.time();source=row(now,'bilibili_live')|{'live_status':1,'session':'12345:1000'}
    enable(engine.content_root,[source]);role_activity.sync(engine.content_root,[source],now)


@pytest.mark.asyncio
async def test_conflict_is_rewritten_once_before_send_and_traces_remain_honest(rig):
    engine,settings,_,_=rig;bind(engine)
    model=Model([{'text':'刚下播，正瘫着呢。','mode':'voice'}, {'text':'我正在直播，抽空回你两句。','mode':'text'}]);engine.provider_override=model
    reply=await engine.handle(event(text='在干嘛'))
    assert reply['text']=='我正在直播，抽空回你两句。' and len(model.calls)==2
    for messages in model.calls:
        data=context(messages)
        assert data['role_activity']['live'][0]['status']=='live'
        assert json.loads(messages[-1]['content'].split('\n',1)[1])['current_message']['text']=='在干嘛'
        assert '当前状态比旧聊天' in messages[0]['content']
    assert context(model.calls[1])['activity_correction']['expected']=='live'
    engine.acknowledge(reply['reply_id'],'sent','999')
    traces=engine.trajectories.list(kind='reply')['items'];attach_delivery(engine,traces)
    assert traces[0]['status']=='success' and traces[0]['delivery']['state']=='sent'
    assert traces[1]['status']=='activity_conflict' and traces[1]['delivery']['state']=='not_sent'
    assert traces[0]['related_id']==traces[1]['id']
    assert traces[1]['activity_check']['expected']=='live'


@pytest.mark.asyncio
@pytest.mark.parametrize('single_call',[False,True])
async def test_conflict_never_loops_or_backs_off_as_network_failure(rig,single_call):
    engine,settings,_,_=rig;bind(engine)
    # Cloud accounting is real, the provider is an in-process fixture.
    write_settings(engine.root,settings.model_copy(update={'provider_mode':'cloud'}))
    if single_call:
        cfg=conversation_config.describe(engine.content_root)
        conversation_config.save(engine.content_root,cfg['config']|{'execution':{'max_model_calls':1,'max_tool_rounds':0}},cfg['revision'])
    model=Model([{'text':'刚下播。','mode':'text'}]*2);engine.provider_override=model
    reply=await engine.handle(event(text='现在在干嘛'))
    assert reply['mode']=='none' and reply['reason']=='activity_state_conflict'
    assert len(model.calls)==(1 if single_call else 2) and engine._cloud_failure_count==0
    assert engine._group_pending=={}


@pytest.mark.asyncio
async def test_correct_or_unrelated_answer_has_no_extra_model_call(rig):
    engine,_,_,_=rig;bind(engine)
    model=Model([{'text':'正播着呢，怎么了？','mode':'text'}]);engine.provider_override=model
    reply=await engine.handle(event(text='在干嘛'))
    assert reply['mode']=='text' and len(model.calls)==1


@pytest.mark.parametrize('manual',[False,True])
@pytest.mark.parametrize('repeat_conflict',[False,True])
async def test_idle_uses_same_bounded_repair_and_honest_delivery(idle_rig,manual,repeat_conflict):
    engine,_,_,_=idle_rig;bind(engine)
    answers=[{'participate':True,'confidence':.95,'text':t,'mode':'text'}
             for t in ['刚下播。','刚下播。' if repeat_conflict else '我正在直播，抽空过来看看。']]
    model=IdleModel(answers);engine.provider_override=model
    if manual:
        engine.idle.queue_manual(SCOPE,'a'*32)
        items=await engine.idle.claim_manual(SELF)
    else:
        await ready(idle_rig)
        items=await engine.idle.claim(SELF)
    assert len(model.calls)==(2 if manual else 3)
    assert bool(items) is not repeat_conflict
    assert engine._cloud_failure_count==0
    if items:assert items[0]['text']==answers[1]['text']
    traces=engine.trajectories.list(kind='group_idle_manual' if manual else 'group_idle')['items']
    attach_delivery(engine,traces)
    assert traces[-1]['status']=='activity_conflict' and traces[-1]['delivery']['state']=='not_sent'


async def test_idle_silent_candidate_does_not_spend_repair_call(idle_rig):
    engine,_,_,_=idle_rig;bind(engine)
    model=IdleModel([{'text':'刚下播。','mode':'text'}],action='observe');engine.provider_override=model
    await ready(idle_rig)
    assert await engine.idle.claim(SELF)==[] and len(model.calls)==1
