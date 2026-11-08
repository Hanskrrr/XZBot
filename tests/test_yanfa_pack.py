"""Exercise the complete imported pack using the real Chinese FTS retrieval."""
import json
import pytest
from pathlib import Path
from azusa_bot import lore

PACK=Path(__file__).resolve().parents[1]/'examples/knowledge/yanfa'


def test_all_published_glosses_import_and_exact_meme_queries_retrieve(tmp_path):
    if not (PACK/'batches').is_dir():pytest.skip("Optional locally imported corpus is not distributed with source")
    docs=[]
    for path in sorted((PACK/'batches').glob('*.json')):
        batch=json.loads(path.read_text())['documents']
        lore.import_documents(tmp_path,batch)
        docs.extend(batch)
    assert len(docs)==271 and len({d['name'] for d in docs})==271
    assert all(d['category']=='reference' and d['always_include'] is False for d in docs)
    assert lore.list_documents(tmp_path)['total']==271
    for query,title in [('圣枪大师','圣枪大师'),('宙活','宙活'),('有一说一','有一说一'),
                        ('他的XX我很怕','他的 XX'),('许哥不是区','许哥不是区'),('红温','红温'),('1557','1557')]:
        found=lore.for_prompt(tmp_path,query)
        assert any(title in d['title'] for d in found),(query,[d['title'] for d in found])
    assert any('炫神' in d['text'] for d in lore.for_prompt(tmp_path,'炫狗 圣枪大师'))
    assert lore.for_prompt(tmp_path,'门锁电池怎么更换')==[]
    assert lore.search(tmp_path,'怎么')  # Management lookup remains literal.
    # FTS retrieves possible lexical matches, not certified semantic relevance:
    # a fruit name also names CaoMei. Keep this limitation explicit.
    ambiguous=lore.for_prompt(tmp_path,'今天想吃草莓蛋糕')
    assert any('抗压王' in row['title'] for row in ambiguous)
    assert all(row['category']=='reference' for row in ambiguous)
