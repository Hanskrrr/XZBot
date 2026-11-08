#!/usr/bin/env python3
"""Build a source-linked glossary pack from the reviewed catalog, offline.

The catalog contains our short glosses, labels, and provenance, not upstream
article bodies. No website/LLM calls, runtime imports, or persona changes.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / 'examples/knowledge/yanfa'


def documents():
    catalog = json.loads((PACK/'catalog.json').read_text())
    result = []
    for row in catalog['entries']:
        tags = list(dict.fromkeys(['研发.lol', '英雄联盟', *row['tags'], *row['aliases']]))[:40]
        if row['focus']: tags += ['炫神', '炫狗']
        labels = '、'.join(row['aliases']) or '无额外别名'
        text = (f"# {row['title']}\n\n相关叫法：{labels}\n\n{row['gloss']}\n\n"
                '资料性质：社区梗的简释索引，未经逐条独立事实核验；不属于角色亲历、现实人物关系或实时新闻。\n'
                f"原词条：{row['url']}\n")
        result.append({'name':f"yanfa-{row['slug']}.md",'title':f"研发.lol｜{row['title']}",
            'text':text,'category':'reference','collection':'研发.lol 梗库','always_include':False,'tags':tags,
            'sources':[row['url'],*row['sources'][:6]],
            'attribution':'研发.lol / debugtheworldbot/lol-meme-wiki 提供条目与线索；XZBot 另写简释。列出的外链未逐页核验。',
            'license':'未发现源站全文转载许可。本包为简释和链接索引，未收录原文正文。',
            'source_revision':catalog['upstream_revision'],'source_retrieved_at':catalog['retrieved_at'],
            'source_format':'plain','source_sha256':row['source_sha256']})
    if len(result)!=271 or len({d['name'] for d in result})!=271:
        raise ValueError('catalog_expected_271_unique_published_entries')
    return result


def main():
    docs=documents();batches=PACK/'batches';batches.mkdir(parents=True,exist_ok=True)
    for start in range(0,len(docs),20):
        (batches/f'{start//20+1:02d}.json').write_text(json.dumps({'documents':docs[start:start+20]},ensure_ascii=False,indent=2)+'\n')
    catalog=json.loads((PACK/'catalog.json').read_text())
    lines=['# 研发.lol 梗库索引（271 条）','',
           '每个条目在 XZBot 中独立检索；这份总目录仅供阅读。未复制源站全文，也未逐条证实赛事史。',
           '', '## 炫神 / 炫狗相关入口', '']
    for row in catalog['entries']:
        if row['focus']:lines.append(f"- [{row['title']}]({row['url']})：{row['gloss']}")
    lines += ['', '## 完整目录', '']
    for row in catalog['entries']:
        lines += [f"### [{row['title']}]({row['url']})",'',row['gloss'],'']
    (PACK/'INDEX.md').write_text('\n'.join(lines))
    names='；'.join(r['title'] for r in catalog['entries'])
    compact='# LOL 梗目录（271 条）\n\n这是可用梗的名称目录；名称不等于已核实的事件或角色亲历。详细释义、别名和来源在独立词条中。\n\n炫神相关：'+ '；'.join(r['title'] for r in catalog['entries'] if r['focus'])+'。许哥指 ShowMaker，Xun 不是炫神。\n\n完整名称索引：\n'+names+'\n'
    (PACK/'DIRECTORY.md').write_text(compact)
    (PACK/'directory-import.json').write_text(json.dumps({'documents':[{'name':'yanfa-directory.md','title':'LOL 梗目录 · 271 条名称','text':compact,'category':'reference','collection':'研发.lol 梗库','always_include':True,'tags':['LOL','梗目录'],'sources':['https://yanfa.lol/memes']}]},ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'entries':len(docs),'batches':14,'focus_entries':sum(r['focus'] for r in catalog['entries'])}))


if __name__=='__main__':main()
