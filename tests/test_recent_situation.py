import json
import time
from types import SimpleNamespace
import pytest
from azusa_bot import role_activity, lore
from azusa_bot.context.recent import assessment, clock
from azusa_bot.prompts import DEFAULTS
from azusa_bot.settings import Settings
from test_role_activity import row, enable
from test_web_search import rig,event


def test_titles_do_not_compete_with_post_quota_and_expire(tmp_path,monkeypatch):
    now=time.time();video=row(now,'bilibili_video');post=row(now)
    enable(tmp_path,[video,post])
    events=[{'published_at':now-60*i,'item_type':'DYNAMIC_TYPE_AV',
             'video':{'bvid':f'BV{i:010d}','title':f'合成角色片段 {i}'}} for i in range(1,13)]
    video['payload']=json.dumps({'events':events});role_activity.sync(tmp_path,[video,post],now)
    rows=[r for r in lore.for_prompt(tmp_path,'无关问题') if r.get('selection_reason')=='recent_activity']
    assert len(rows)==13
    clips=[r for r in rows if r['activity_source'].startswith('bilibili_video:')]
    assert len(clips)==12 and all('发布时间' in r['text'] for r in clips)
    recent=assessment(rows,clock(now));assert len(recent['events'])==13
    assert recent['evaluated_at']['local'].endswith('+08:00')
    assert all(e['content_event_time']=='not_inferred' for e in recent['events'])
    assert not assessment(rows,clock(now+86400))['events']
    doc=clips[0]['doc_id'];lore.delete_document(tmp_path,doc)
    assert doc not in {r['doc_id'] for r in lore.for_prompt(tmp_path,'无关问题')}


def test_keyword_zero_match_is_visible_and_can_be_corrected(tmp_path):
    now=time.time();video=row(now,'bilibili_video');enable(tmp_path,[video])
    video['payload']=json.dumps({'events':[{'published_at':now-10,'item_type':'DYNAMIC_TYPE_AV',
        'video':{'bvid':'BV0000000001','title':'合成角色玩游戏'}}]})
    cfg=role_activity.config(tmp_path)
    role_activity.save(tmp_path,{'enabled':True,'sources':[video['key']],
        'revision':role_activity.revision(cfg),'clip_keywords':{video['key']:['作者名字']}},[video])
    role_activity.sync(tmp_path,[video],now)
    report=role_activity.describe(tmp_path,[video])['filter_diagnostics'][0]
    assert (report['recent_count'],report['matched_count'],report['filtered_count'])==(1,0,1)
    cfg=role_activity.config(tmp_path)
    role_activity.save(tmp_path,{'enabled':True,'sources':[video['key']],
        'revision':role_activity.revision(cfg),'clip_keywords':{video['key']:['合成角色']}},[video])
    role_activity.sync(tmp_path,[video],now)
    assert len(role_activity.describe(tmp_path,[video])['context_preview']['items'])==1


@pytest.mark.parametrize('kind',['reply','group_decision','followup','group_idle','group_idle_manual'])
def test_every_reply_path_gets_current_clock_even_without_activity(tmp_path,kind):
    prefix=DEFAULTS['direct_header' if kind=='reply' else 'group_header']
    messages=[{'role':'system','content':'角色约定'},{'role':'user','content':prefix+json.dumps({'lore':[]})}]
    refs={};result=role_activity.augment(SimpleNamespace(content_root=tmp_path),messages,Settings(),{'kind':kind,'refs':refs})
    data=json.loads(result[1]['content'][len(prefix):])
    assert data['current_time']['timezone']=='Asia/Shanghai'
    assert data['recent_situation']['status']=='no_recent_records'
    assert refs['recent_situation']==data['recent_situation']
    assert '此刻' in result[0]['content'] or '现在的时间' in result[0]['content']


@pytest.mark.asyncio
async def test_recent_titles_and_time_reach_real_provider_boundary_and_tool_round(rig):
    engine,_,model,calls=rig;now=time.time();video=row(now,'bilibili_video')
    enable(engine.content_root,[video])
    video['payload']=json.dumps({'events':[{'published_at':now-60,'item_type':'DYNAMIC_TYPE_AV',
        'video':{'bvid':'BV0000000001','title':'合成角色刚发布的片段'}}]})
    role_activity.sync(engine.content_root,[video],now)
    await engine.handle(event())
    assert len(model.calls)==2 and len(calls)==1
    for messages in model.calls:
        data=json.loads(messages[1]['content'].split('\n',1)[1])
        assert data['current_time']['timezone']=='Asia/Shanghai'
        assert len(data['recent_situation']['events'])==1
        assert any('合成角色刚发布的片段' in r['text'] for r in data['lore'])
        assert 'current_message' in json.loads(messages[-1]['content'].split('\n',1)[1])
