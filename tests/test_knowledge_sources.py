"""Public article ingestion uses synthetic HTML; no cloud or account access."""
import json
import httpx
import pytest

from azusa_bot import knowledge_sources as source, lore


def page():
    metadata = {"titletext": "测试角色", "isarticle": True,
        "copyright": '<a rel="license" href="https://creativecommons.org/licenses/by-nc-sa/4.0/">CC BY-NC-SA 4.0</a>'}
    return ('<html><title>测试角色 - 萌娘百科</title><script>{"wgRevisionId":12345}</script>'
        '<script id="MOE_SKIN_PAGE_DATA" type="application/json">' + json.dumps(metadata) + '</script>'
        '<template id="MOE_SKIN_TEMPLATE_BODYCONTENT"><div id="mw-content-text"><div class="mw-parser-output">'
        '<style>.x{bad:style}</style><script>BAD_SCRIPT</script><div id="toc">目录噪音</div>'
        '<h2>历史原文</h2><p>以下全部为离线测试文本。这位虚构角色在蓝色灯塔发生的事情，必须保留原句，不能生成摘要。</p>'
        '<p>原句包含<s>这个玩笑不能当真</s>与原来的说明。</p><ul><li>事件甲</li><li>事件乙</li></ul>'
        '</div></div></template></html>')


def test_template_article_keeps_original_words_and_attribution(tmp_path):
    result = source.parse_moegirl(page(), "https://zh.moegirl.org.cn/测试角色")
    doc = result['document']
    assert '以下全部为离线测试文本。这位虚构角色在蓝色灯塔发生的事情，必须保留原句，不能生成摘要。' in doc['text']
    assert '~~这个玩笑不能当真~~' in doc['text']
    assert all(noise not in doc['text'] for noise in ['BAD_SCRIPT', '目录噪音', 'bad:style'])
    assert doc['source_revision'] == '12345' and doc['license'] == 'CC BY-NC-SA 4.0'
    added = lore.import_documents(tmp_path, [doc])['documents'][0]
    readback = lore.get_document(tmp_path, added['id'])
    assert readback['text'] == doc['text']
    hit = lore.search(tmp_path, '蓝色灯塔')[0]
    assert hit['category'] == 'reference' and hit['attribution'] == doc['attribution']
    assert hit['source_revision'] == '12345'
    lore.update_document(tmp_path, added['id'], {'text':doc['text']+'\n自己补充的内容。'})
    assert lore.get_document(tmp_path, added['id'])['source_sha256'] == doc['source_sha256']


@pytest.mark.parametrize('url', ['http://zh.moegirl.org.cn/阿梓', 'https://127.0.0.1/阿梓',
    'https://zh.moegirl.org.cn.evil.example/阿梓', 'https://secret@zh.moegirl.org.cn/阿梓',
    'https://zh.moegirl.org.cn:8080/阿梓', 'https://zh.moegirl.org.cn/api.php',
    'https://zh.moegirl.org.cn/阿梓?action=delete', 'https://zh.moegirl.org.cn/阿梓?oldid=bad',
    'https://zh.moegirl.org.cn/阿梓?title=甲&title=乙', 'https://zh.moegirl.org.cn/%0a'])
def test_source_rejects_arbitrary_hosts_actions_and_invalid_titles(url):
    with pytest.raises(ValueError):
        source.article_url(url)


def test_known_article_and_old_revision_urls_are_canonical():
    assert source.article_url('https://mzh.moegirl.org.cn/index.php?title=阿梓&oldid=123#梗') == 'https://zh.moegirl.org.cn/%E9%98%BF%E6%A2%93?oldid=123'


@pytest.mark.parametrize('content,error', [('<html>Please enable JavaScript</html>','source_access_restricted'),
    (page().replace('by-nc-sa/4.0/','all-rights-reserved/'),'source_license_not_recognized')])
def test_challenges_and_unknown_licenses_do_not_become_knowledge(content,error):
    with pytest.raises(ValueError, match=error): source.parse_moegirl(content,'https://zh.moegirl.org.cn/测试角色')


@pytest.mark.asyncio
async def test_bounded_fetch_never_follows_an_external_redirect(monkeypatch):
    actual = httpx.AsyncClient
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302,headers={'location':'https://127.0.0.1/admin'})
    monkeypatch.setattr(source.httpx,'AsyncClient',lambda **kw: actual(transport=httpx.MockTransport(handler),**kw))
    with pytest.raises(ValueError): await source.fetch_moegirl('https://zh.moegirl.org.cn/测试角色')
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_public_html_fetch_has_no_cookie_or_api_call(monkeypatch):
    actual = httpx.AsyncClient
    def handler(request):
        assert request.url.host == 'zh.moegirl.org.cn' and not request.url.path.endswith('api.php')
        assert 'cookie' not in request.headers and 'authorization' not in request.headers
        return httpx.Response(200,text=page(),headers={'content-type':'text/html;charset=utf-8'})
    monkeypatch.setattr(source.httpx,'AsyncClient',lambda **kw: actual(transport=httpx.MockTransport(handler),**kw))
    assert (await source.fetch_moegirl('https://zh.moegirl.org.cn/测试角色'))['document']['source_format'] == 'html_text'


@pytest.mark.parametrize('bad', [{'source_sha256':'bad'}, {'source_format':'script'},
    {'source_retrieved_at':'2026-11-07'}, {'attribution':123}, {'license':'bad\nvalue'}])
def test_import_provenance_is_validated(tmp_path,bad):
    doc=source.parse_moegirl(page(),'https://zh.moegirl.org.cn/测试角色')['document']
    with pytest.raises(ValueError): lore.import_documents(tmp_path,[dict(doc,**bad)])


def test_preview_requires_admin_and_never_imports_until_explicit_save(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from azusa_bot.app import create_app
    from azusa_bot.settings import Settings, write_settings
    (tmp_path/'content').mkdir()
    (tmp_path/'content/persona.md').write_text('用于离线检验的原创角色人设。')
    write_settings(tmp_path,Settings())
    calls=[]
    async def fake(url):
        calls.append(url)
        if url == 'blocked': raise ValueError('source_access_restricted')
        return source.parse_moegirl(page(),url)
    monkeypatch.setattr(source,'fetch_moegirl',fake)
    app=create_app(tmp_path)
    headers={'Authorization':'Bearer '+(tmp_path/'data/admin.token').read_text()}
    with TestClient(app) as client:
        assert client.post('/admin/knowledge/source/preview',json={'url':'https://zh.moegirl.org.cn/测试角色'}).status_code==401
        assert calls==[]
        preview=client.post('/admin/knowledge/source/preview',headers=headers,json={'url':'https://zh.moegirl.org.cn/测试角色'})
        assert preview.status_code==200
        assert client.get('/admin/knowledge',headers=headers).json()['total']==0
        doc=preview.json()['document']
        saved=client.post('/admin/knowledge/import',headers=headers,json={'documents':[doc]})
        assert saved.status_code==200
        retrieved=client.get('/admin/knowledge/'+saved.json()['documents'][0]['id'],headers=headers).json()
        assert retrieved['text']==doc['text'] and retrieved['source_revision']=='12345'
        blocked=client.post('/admin/knowledge/source/preview',headers=headers,json={'url':'blocked'})
        assert blocked.status_code==400 and blocked.json()['detail']=='source_access_restricted'
    app.state.engine.close()
