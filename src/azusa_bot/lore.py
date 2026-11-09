"""Local FTS5 knowledge: atomic source manifest, rebuildable index, no network."""
from __future__ import annotations
from .host import safe_open, NOFOLLOW
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime
from .host import file_locks as fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import threading
import time
from urllib.parse import urlsplit
import uuid
import yaml

MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_BATCH_DOCUMENTS = 20
MAX_BATCH_BYTES = 6 * 1024 * 1024
MAX_DOCUMENTS = 2000
MAX_ALWAYS_DOCUMENTS = 20
MAX_LIBRARY_BYTES = 128 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
CHUNK_CHARS, CHUNK_OVERLAP = 1200, 140
CATEGORIES = {"canon", "verified", "reference", "fanwork", "fiction", "unverified"}
_DOC_ID = re.compile(r"(?:[a-f0-9]{32}|legacy-[a-f0-9]{24})\Z")
_BLOB = re.compile(r"(?:[a-f0-9]{32}|legacy-[a-f0-9]{24})-[a-f0-9]{64}\.txt\Z")
_WORDS = re.compile(r"[a-z0-9]+|[\u3400-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f]+")
_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f]")
_TOKENIZER_VERSION = 2
_METADATA_VERSION = 4
_PROVENANCE_FIELDS = {"attribution", "license", "source_revision", "source_retrieved_at", "source_format", "source_sha256"}
_FIELDS = {"name", "text", "content", "title", "category", "sources", "event_date", "tags", "always_include", "collection", "context_until", "activity_source"} | _PROVENANCE_FIELDS
_STORES, _REGISTRY_LOCK = {}, threading.Lock()


def _tokens(text):
    result = []
    for word in _WORDS.findall(text.casefold()):
        if re.fullmatch(r"[a-z0-9]+", word) or len(word) == 1:
            result.append(word)
        else:
            result.extend(word[i:i+2] for i in range(len(word)-1))
    return result


def terms(text: str) -> set[str]:
    return set(_tokens(text))


def _index_tokens(text):
    # Index each individual Han character for explicitly one-character queries.
    # Queries of two or more Han characters still expand to bigrams only.
    return _tokens(text) + sorted(set(_CJK.findall(text)))


# Remove only grammatical/filler matches from automatic prompt retrieval.
# Admin search remains literal so an editor can still find such wording.
# This is not semantic disambiguation: proper-name homonyms remain candidates.
_PROMPT_NOISE = frozenset(_tokens(
    '怎么 什么 为什么 如何 这个 那个 这些 那些 一个 一些 你们 我们 他们 大家 '
    '今天 现在 可以 可能 还是 就是 不是 已经 觉得 因为 所以 但是 其实 '
    'http https www com'))


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stamp(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("knowledge_file_must_be_regular_not_symlink")
    return info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _read(path, maximum):
    try:
        descriptor = safe_open(path, os.O_RDONLY | NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
                raise ValueError("knowledge_file_invalid_or_too_large")
            value = stream.read(maximum + 1)
    except OSError:
        raise ValueError("knowledge_file_unavailable_or_symlink") from None
    if len(value) > maximum:
        raise ValueError("knowledge_file_too_large")
    return value


def _directory(path):
    if path.exists() or path.is_symlink():
        if not stat.S_ISDIR(path.lstat().st_mode):
            raise ValueError("knowledge_directory_must_not_be_symlink")
    else:
        path.mkdir(mode=0o700)


def _atomic(path, data):
    _stamp(path)
    descriptor, name = tempfile.mkstemp(prefix=".knowledge-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _decode(value):
    if isinstance(value, bytes):
        if len(value) > MAX_DOCUMENT_BYTES:
            raise ValueError("knowledge_document_exceeds_1mib")
        try:
            value = value.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValueError("knowledge_document_requires_utf8") from None
    if not isinstance(value, str):
        raise ValueError("knowledge_document_requires_text")
    value = value.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("knowledge_document_requires_utf8") from None
    if size > MAX_DOCUMENT_BYTES or not value.strip() or "\x00" in value:
        raise ValueError("knowledge_document_empty_binary_or_too_large")
    return value


def _frontmatter(text):
    if not text.startswith("---\n"):
        return {}, text, 0
    end = re.search(r"^---[ \t]*$", text[4:32772], flags=re.MULTILINE)
    if not end:
        raise ValueError("knowledge_frontmatter_missing_or_too_large")
    try:
        meta = yaml.safe_load(text[4:4+end.start()]) or {}
    except yaml.YAMLError:
        raise ValueError("knowledge_frontmatter_invalid") from None
    if not isinstance(meta, dict):
        raise ValueError("knowledge_frontmatter_must_be_object")
    offset = 4 + end.end()
    while offset < len(text) and text[offset] in "\r\n":
        offset += 1
    return meta, text[offset:], offset


def _name(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 200
        or "/" in value or "\\" in value or any(ord(c) < 32 for c in value)
        or Path(value).suffix.lower() not in {".md", ".txt"}):
        raise ValueError("knowledge_filename_requires_plain_md_or_txt_name")
    return value


def _metadata(item, text, old=None):
    if not isinstance(item, dict) or set(item) - _FIELDS:
        raise ValueError("knowledge_document_fields_invalid")
    front, body, _ = _frontmatter(text)
    if not body.strip():
        raise ValueError("knowledge_document_body_empty")
    old = old or {}
    collection=item.get('collection',old.get('collection',front.get('collection','')))
    if not isinstance(collection,str) or len(collection)>80 or any(ord(c)<32 for c in collection):
        raise ValueError('knowledge_collection_invalid')
    until=item.get('context_until',old.get('context_until',0))
    if type(until) not in (int,float) or not 0<=until<1e11:raise ValueError('knowledge_context_until_invalid')
    activity=item.get('activity_source',old.get('activity_source',''))
    if not isinstance(activity,str) or activity and not re.fullmatch(r'(?:bilibili_live|bilibili_dynamic|bilibili_video|weibo):[0-9]{1,20}',activity):
        raise ValueError('knowledge_activity_source_invalid')
    name = _name(item.get("name", old.get("name", "document.md")))
    title = item.get("title", old.get("title", front.get("title", Path(name).stem)))
    category = item.get("category", old.get("category", front.get("category", "unverified")))
    sources = item.get("sources", old.get("sources", front.get("sources", [])))
    always_include = item.get("always_include", old.get("always_include", front.get("always_include", False)))
    tags = item.get("tags", old.get("tags", front.get("tags", front.get("keywords", []))))
    event_date = item.get("event_date", old.get("event_date", front.get("event_date")))
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 200 or any(ord(c)<32 for c in title):
        raise ValueError("knowledge_title_invalid")
    if not isinstance(category, str) or category not in CATEGORIES:
        raise ValueError("knowledge_category_invalid")
    if type(always_include) is not bool:
        raise ValueError("knowledge_always_include_invalid")
    if always_include and category == "unverified":
        raise ValueError("knowledge_always_include_unverified")
    if not isinstance(sources, list) or len(sources) > 20:
        raise ValueError("knowledge_sources_invalid")
    checked = []
    for source in sources:
        if not isinstance(source, str) or len(source) > 2048 or any(c.isspace() or ord(c)<32 for c in source):
            raise ValueError("knowledge_source_url_invalid")
        try:
            parsed = urlsplit(source)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError()
            parsed.port
        except ValueError:
            raise ValueError("knowledge_source_url_invalid") from None
        if source not in checked:
            checked.append(source)
    if not isinstance(tags, list) or len(tags) > 40 or any(not isinstance(t,str) or not 1<=len(t.strip())<=80 or any(ord(c)<32 for c in t) for t in tags):
        raise ValueError("knowledge_tags_invalid")
    if isinstance(event_date, date):
        event_date = event_date.isoformat()
    if event_date in (None, ""):
        event_date = None
    else:
        try:
            if not isinstance(event_date,str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", event_date):
                raise ValueError()
            date.fromisoformat(event_date)
        except ValueError:
            raise ValueError("knowledge_event_date_invalid") from None
    provenance = {}
    for key in _PROVENANCE_FIELDS:
        value = item.get(key, old.get(key, front.get(key, "")))
        if not isinstance(value, str) or len(value) > (2000 if key == "attribution" else 256) or any(ord(c) < 32 for c in value):
            raise ValueError("knowledge_provenance_invalid")
        if key == "source_format" and value not in {"", "plain", "wikitext", "html_text"}:
            raise ValueError("knowledge_source_format_invalid")
        if key == "source_sha256" and value and not re.fullmatch(r"[a-f0-9]{64}", value):
            raise ValueError("knowledge_source_hash_invalid")
        if key == "source_retrieved_at" and value:
            try:
                if datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
                    raise ValueError()
            except ValueError:
                raise ValueError("knowledge_source_timestamp_invalid") from None
        if value:
            provenance[key] = value
    return dict(name=name,title=title.strip(),category=category,sources=checked,always_include=always_include,
                collection=collection.strip(),context_until=until,activity_source=activity,
                tags=list(dict.fromkeys(t.strip() for t in tags)),event_date=event_date,**provenance)


def _chunks(text):
    _, body, base = _frontmatter(text)
    start = 0
    while start < len(body):
        end = min(start + CHUNK_CHARS, len(body))
        if end < len(body):
            boundary = max(body.rfind("\n", start+400, end), body.rfind("。", start+400, end))
            if boundary >= 0:
                end = boundary + 1
        raw = body[start:end]
        left = start + len(raw) - len(raw.lstrip())
        right = end - (len(raw) - len(raw.rstrip()))
        if right > left:
            yield base+left, base+right, body[left:right]
        if end >= len(body):
            break
        start = max(start+1, end-CHUNK_OVERLAP)


class _Index:
    def __init__(self, root):
        self.root = root
        self.home = root / "content/knowledge"
        self.blobs = self.home / "documents"
        self.manifest = self.home / "manifest.json"
        self.database = root / "data/knowledge.sqlite3"
        self.lock, self.depth = threading.RLock(), 0

    @contextmanager
    def locked(self):
        with self.lock:
            if self.depth:
                self.depth += 1
                try:
                    yield
                finally:
                    self.depth -= 1
                return
            for path in (self.root,self.root/"content",self.home,self.blobs,self.root/"data"):
                _directory(path)
            try:
                descriptor = safe_open(self.home/".lock", os.O_CREAT | os.O_RDWR | NOFOLLOW, 0o600)
            except OSError:
                raise ValueError("knowledge_lock_unavailable_or_symlink") from None
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ValueError("knowledge_lock_invalid")
                fcntl.flock(descriptor,fcntl.LOCK_EX)
                self.depth = 1
                yield
            finally:
                self.depth = 0
                os.close(descriptor)

    def read_manifest(self):
        if _stamp(self.manifest) is None:
            return {"schema":1,"documents":{},"hidden_legacy":[]}
        try:
            data = json.loads(_read(self.manifest,MAX_MANIFEST_BYTES))
        except (ValueError,UnicodeError):
            raise ValueError("knowledge_manifest_invalid") from None
        if (not isinstance(data,dict) or data.get("schema") != 1 or not isinstance(data.get("documents"),dict)
            or len(data["documents"])>MAX_DOCUMENTS or not isinstance(data.get("hidden_legacy"),list)):
            raise ValueError("knowledge_manifest_invalid")
        for doc_id, record in data["documents"].items():
            if (not _DOC_ID.fullmatch(doc_id) or not isinstance(record,dict) or record.get("id") != doc_id
                or not _BLOB.fullmatch(str(record.get("file","")))
                or record["file"] != doc_id+"-"+str(record.get("content_hash"))+".txt"):
                raise ValueError("knowledge_manifest_document_invalid")
        if any(not isinstance(x,str) or not x.startswith("legacy-") or not _DOC_ID.fullmatch(x) for x in data["hidden_legacy"]):
            raise ValueError("knowledge_manifest_hidden_id_invalid")
        return data

    def sources(self):
        legacy = self.root/"content/lore"
        paths = []
        if legacy.exists() or legacy.is_symlink():
            _directory(legacy)
            paths = sorted(p for p in legacy.iterdir() if p.suffix.lower() in {".md",".txt"})
        if len(paths)>MAX_DOCUMENTS:
            raise ValueError("knowledge_legacy_document_limit")
        signature = [_TOKENIZER_VERSION,_METADATA_VERSION,_stamp(self.manifest),[(p.name,_stamp(p)) for p in paths]]
        return _hash(_json(signature).encode()), paths

    @contextmanager
    def db(self):
        if _stamp(self.database) is None:
            descriptor = safe_open(self.database,os.O_CREAT | os.O_EXCL | os.O_WRONLY | NOFOLLOW,0o600)
            os.close(descriptor)
        for suffix in ("-journal","-wal","-shm"):
            _stamp(Path(str(self.database)+suffix))
        connection = sqlite3.connect(self.database,timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA secure_delete=ON")
        try:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS knowledge_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY,revision TEXT,category TEXT,metadata TEXT);
                CREATE TABLE IF NOT EXISTS chunks(rowid INTEGER PRIMARY KEY,chunk_id TEXT UNIQUE,doc_id TEXT,
                    ordinal INTEGER,start_char INTEGER,end_char INTEGER,text TEXT);
                CREATE INDEX IF NOT EXISTS chunk_document ON chunks(doc_id);
                CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(title,tags,body,tokenize='unicode61 remove_diacritics 0');
            """)
            with connection:
                yield connection
        finally:
            connection.close()

    def records(self, paths, manifest):
        records = {doc_id:dict(record,always_include=record.get("always_include",False))
                   for doc_id,record in manifest["documents"].items()}
        legacy_text = {}
        for record in records.values():
            if type(record["always_include"]) is not bool:
                raise ValueError("knowledge_always_include_invalid")
            if record["always_include"] and record["category"] == "unverified":
                raise ValueError("knowledge_always_include_unverified")
        hidden = set(manifest["hidden_legacy"])
        for path in paths:
            doc_id = "legacy-"+_hash(path.name.encode())[:24]
            if doc_id in records or doc_id in hidden:
                continue
            try:
                text = _decode(_read(path,MAX_DOCUMENT_BYTES))
                front,_,_ = _frontmatter(text)
                meta = _metadata({"name":path.name},text)
            except ValueError as error:
                if str(error).startswith(("knowledge_file_","knowledge_always_include_")):
                    raise
                continue
            info = path.stat()
            records[doc_id] = dict(meta,id=doc_id,origin="legacy",legacy_id=str(front.get("id",path.stem))[:200],
                content_hash=_hash(text.encode()),bytes=len(text.encode()),created_at=info.st_ctime,updated_at=info.st_mtime)
            legacy_text[doc_id] = text
        if len(records)>MAX_DOCUMENTS or sum(r.get("bytes",0) for r in records.values())>MAX_LIBRARY_BYTES:
            raise ValueError("knowledge_library_limit")
        if sum(r["always_include"] for r in records.values()) > MAX_ALWAYS_DOCUMENTS:
            raise ValueError("knowledge_always_include_limit")
        return records,legacy_text

    def sync(self, db, force=False):
        signature, paths = self.sources()
        row = db.execute("SELECT value FROM knowledge_meta WHERE key='source_signature'").fetchone()
        if not force and row and row[0]==signature:
            return
        records, legacy_text = self.records(paths,self.read_manifest())
        existing = {r["id"]:r["revision"] for r in db.execute("SELECT id,revision FROM documents")}
        db.execute("BEGIN IMMEDIATE")
        for doc_id in set(existing)-set(records):
            self.remove_index(db,doc_id)
        for doc_id, record in records.items():
            revision = _hash(_json([_TOKENIZER_VERSION,record]).encode())
            if not force and existing.get(doc_id)==revision:
                continue
            text = legacy_text.get(doc_id)
            if text is None:
                text = _decode(_read(self.blobs/record["file"],MAX_DOCUMENT_BYTES))
            if _hash(text.encode())!=record["content_hash"]:
                raise ValueError("knowledge_source_hash_mismatch")
            self.remove_index(db,doc_id)
            db.execute("INSERT INTO documents VALUES (?,?,?,?)",(doc_id,revision,record["category"],_json(record)))
            occurrences = Counter()
            for ordinal,(start,end,chunk) in enumerate(_chunks(text)):
                digest = _hash(chunk.encode())[:20]
                occurrence = occurrences[digest]; occurrences[digest] += 1
                chunk_id = f"{doc_id}:{digest}:{occurrence}"
                cursor = db.execute("INSERT INTO chunks(chunk_id,doc_id,ordinal,start_char,end_char,text) VALUES (?,?,?,?,?,?)",
                    (chunk_id,doc_id,ordinal,start,end,chunk))
                db.execute("INSERT INTO chunk_fts(rowid,title,tags,body) VALUES (?,?,?,?)",(cursor.lastrowid,
                    " ".join(_index_tokens(record["title"]))," ".join(_index_tokens(" ".join(record["tags"])))," ".join(_index_tokens(chunk))))
        db.execute("INSERT OR REPLACE INTO knowledge_meta VALUES ('source_signature',?)",(signature,))

    @staticmethod
    def remove_index(db, doc_id):
        db.execute("DELETE FROM chunk_fts WHERE rowid IN (SELECT rowid FROM chunks WHERE doc_id=?)",(doc_id,))
        db.execute("DELETE FROM chunks WHERE doc_id=?",(doc_id,))
        db.execute("DELETE FROM documents WHERE id=?",(doc_id,))

    @staticmethod
    def public(record):
        return {k:v for k,v in record.items() if k not in {"file","content_hash"}}

    def record(self, db, doc_id):
        if not isinstance(doc_id,str) or not _DOC_ID.fullmatch(doc_id):
            raise ValueError("knowledge_document_id_invalid")
        row = db.execute("SELECT metadata FROM documents WHERE id=?",(doc_id,)).fetchone()
        if row is None:
            raise ValueError("knowledge_document_not_found")
        return json.loads(row[0])

    def text(self, record):
        path = self.root/"content/lore"/_name(record["name"]) if record["origin"]=="legacy" else self.blobs/record["file"]
        return _decode(_read(path,MAX_DOCUMENT_BYTES))

    def blob(self, doc_id, text):
        digest = _hash(text.encode())
        filename = doc_id+"-"+digest+".txt"
        path = self.blobs/filename
        if _stamp(path) is None:
            _atomic(path,text.encode())
        elif _hash(_read(path,MAX_DOCUMENT_BYTES))!=digest:
            raise ValueError("knowledge_source_hash_mismatch")
        return dict(file=filename,content_hash=digest,bytes=len(text.encode()))

    def commit(self, manifest):
        # The manifest is the commit point. A crash before it leaves only ignored
        # orphan blobs; a crash after it is recovered by the next index sync.
        # Ordinary indexing errors roll back the source publication as well.
        encoded = (_json(manifest)+"\n").encode()
        if len(encoded)>MAX_MANIFEST_BYTES:
            raise ValueError("knowledge_manifest_too_large")
        previous = _read(self.manifest,MAX_MANIFEST_BYTES) if _stamp(self.manifest) else None
        _atomic(self.manifest,encoded)
        try:
            with self.db() as db:
                self.sync(db)
        except Exception:
            if previous is None:
                self.manifest.unlink()
            else:
                _atomic(self.manifest,previous)
            raise
        keep = {r["file"] for r in manifest["documents"].values()}
        for path in self.blobs.iterdir():
            if _BLOB.fullmatch(path.name) and path.name not in keep:
                _stamp(path)
                path.unlink()


def _store(root):
    path = Path(root).resolve()
    with _REGISTRY_LOCK:
        if path not in _STORES:
            if len(_STORES)>=8:
                _STORES.pop(next(iter(_STORES)))
            _STORES[path] = _Index(path)
        return _STORES[path]


@contextmanager
def backup_guard(root):
    """Hold while copying content/ to preserve one complete manifest generation."""
    with _store(root).locked():
        yield


def import_documents(root, documents):
    if not isinstance(documents,list) or not 1<=len(documents)<=MAX_BATCH_DOCUMENTS:
        raise ValueError("knowledge_batch_requires_1_to_20_documents")
    prepared = []
    for item in documents:
        if not isinstance(item,dict) or set(item)-_FIELDS or ("text" in item and "content" in item):
            raise ValueError("knowledge_document_fields_invalid")
        text = _decode(item.get("text",item.get("content")))
        prepared.append((_metadata(item,text),text))
    size = sum(len(text.encode()) for _,text in prepared)
    if size>MAX_BATCH_BYTES:
        raise ValueError("knowledge_batch_exceeds_6mib")
    store = _store(root)
    with store.locked():
        with store.db() as db:
            store.sync(db)
            total = db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            indexed = [json.loads(r[0]) for r in db.execute("SELECT metadata FROM documents")]
            used = sum(r["bytes"] for r in indexed)
            if sum(r["always_include"] for r in indexed) + sum(meta["always_include"] for meta,_ in prepared) > MAX_ALWAYS_DOCUMENTS:
                raise ValueError("knowledge_always_include_limit")
        if total+len(prepared)>MAX_DOCUMENTS or used+size>MAX_LIBRARY_BYTES:
            raise ValueError("knowledge_library_limit")
        manifest, imported = store.read_manifest(), []
        for meta,text in prepared:
            doc_id, now = uuid.uuid4().hex, time.time()
            record = dict(meta,id=doc_id,origin="imported",created_at=now,updated_at=now,**store.blob(doc_id,text))
            manifest["documents"][doc_id] = record
            imported.append(store.public(record))
        store.commit(manifest)
        return {"imported":len(imported),"documents":imported,"index_ready":True}


def list_documents(root, limit=100, offset=0, *, collection=None, sort='added_desc'):
    if isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=200 or isinstance(offset,bool) or not isinstance(offset,int) or offset<0:
        raise ValueError("knowledge_pagination_invalid")
    store = _store(root)
    with store.locked(),store.db() as db:
        store.sync(db)
        orders={'added_desc','added_asc','title'}
        if sort not in orders or collection is not None and (not isinstance(collection,str) or len(collection)>80):raise ValueError('knowledge_filter_invalid')
        all_rows=[json.loads(r[0]) for r in db.execute('SELECT metadata FROM documents')]
        counts=Counter(r.get('collection','') for r in all_rows)
        selected=[r for r in all_rows if collection is None or r.get('collection','')==collection]
        if sort=='title':selected.sort(key=lambda r:(r['title'].casefold(),r['id']))
        else:selected.sort(key=lambda r:(r['created_at'],r['id']),reverse=sort=='added_desc')
        return {'total':len(selected),'documents':[store.public(r) for r in selected[offset:offset+limit]],
                'collections':[{'name':k,'count':v} for k,v in sorted(counts.items())]}


def get_document(root, doc_id):
    store = _store(root)
    with store.locked(),store.db() as db:
        store.sync(db)
        record = store.record(db,doc_id)
        return {**store.public(record),"text":store.text(record)}


def move_documents(root, ids, collection):
    if (not isinstance(ids,list) or not 1<=len(ids)<=MAX_DOCUMENTS
        or any(not isinstance(n,str) for n in ids) or len(set(ids))!=len(ids)):
        raise ValueError('knowledge_move_invalid')
    store=_store(root)
    with store.locked():
        with store.db() as db:
            store.sync(db);records=[store.record(db,n) for n in ids]
        manifest=store.read_manifest()
        for old in records:
            text=store.text(old);meta=_metadata({'collection':collection},text,old)
            record=dict(meta,id=old['id'],origin='imported',created_at=old['created_at'],updated_at=time.time(),**store.blob(old['id'],text))
            if old.get('legacy_id'):record['legacy_id']=old['legacy_id']
            manifest['documents'][old['id']]=record
        store.commit(manifest)
    return {'moved':len(ids),'collection':collection}


def update_document(root, doc_id, changes):
    if not isinstance(changes,dict) or set(changes)-_FIELDS or ("text" in changes and "content" in changes):
        raise ValueError("knowledge_document_fields_invalid")
    store = _store(root)
    with store.locked():
        with store.db() as db:
            store.sync(db)
            old = store.record(db,doc_id)
            source = changes["text"] if "text" in changes else changes["content"] if "content" in changes else store.text(old)
            text = _decode(source)
            meta = _metadata(changes,text,old=old)
            indexed = [json.loads(r[0]) for r in db.execute("SELECT metadata FROM documents")]
            used = sum(r["bytes"] for r in indexed)
            if sum(r["always_include"] for r in indexed)-old["always_include"]+meta["always_include"] > MAX_ALWAYS_DOCUMENTS:
                raise ValueError("knowledge_always_include_limit")
        if used-old["bytes"]+len(text.encode())>MAX_LIBRARY_BYTES:
            raise ValueError("knowledge_library_limit")
        manifest = store.read_manifest()
        record = dict(meta,id=doc_id,origin="imported",created_at=old["created_at"],updated_at=time.time(),**store.blob(doc_id,text))
        if old.get("legacy_id"):
            record["legacy_id"] = old["legacy_id"]
        manifest["documents"][doc_id] = record
        store.commit(manifest)
        return store.public(record)


def delete_document(root, doc_id):
    store = _store(root)
    with store.locked():
        with store.db() as db:
            store.sync(db)
            store.record(db,doc_id)
        manifest = store.read_manifest()
        manifest["documents"].pop(doc_id,None)
        if doc_id.startswith("legacy-") and doc_id not in manifest["hidden_legacy"]:
            manifest["hidden_legacy"].append(doc_id)
        store.commit(manifest)
        return {"deleted":True}


def rebuild_index(root):
    store = _store(root)
    with store.locked(),store.db() as db:
        store.sync(db,force=True)
        return {"documents":db.execute("SELECT COUNT(*) FROM documents").fetchone()[0],"chunks":db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],"index_ready":True}


def _validate_query(query, limit, maximum=50):
    if not isinstance(query,str) or len(query)>10000 or isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=maximum:
        raise ValueError("knowledge_search_invalid")


def _search(db, query, limit, excluded=(), *, for_prompt=False, collection=None):
    tokens = list(dict.fromkeys(token for token in _tokens(query)
                               if not for_prompt or token not in _PROMPT_NOISE))[:48]
    if not tokens:
        return []
    match = " OR ".join('"'+token+'"' for token in tokens)
    exclude_sql = (" AND c.doc_id NOT IN ("+",".join("?" for _ in excluded)+")") if excluded else ""
    folder_sql=" AND COALESCE(json_extract(d.metadata,'$.collection'),'')=?" if collection is not None else ''
    # Apply document diversity BEFORE the global limit, excluding full documents
    # in SQL so their duplicate title hits cannot consume retrieval slots.
    rows = db.execute("""WITH matches AS MATERIALIZED (
            SELECT c.rowid,c.doc_id,c.ordinal,bm25(chunk_fts,6.0,4.0,1.0) AS rank
            FROM chunk_fts JOIN chunks c ON c.rowid=chunk_fts.rowid JOIN documents d ON d.id=c.doc_id
            WHERE chunk_fts MATCH ? AND d.category IN ('canon','verified','reference','fanwork','fiction')
            """+exclude_sql+folder_sql+"""
        ), ranked AS (
            SELECT *,row_number() OVER (PARTITION BY doc_id ORDER BY rank,ordinal) AS document_rank
            FROM matches
        )
        SELECT c.*,d.metadata,ranked.rank FROM ranked
        JOIN chunks c ON c.rowid=ranked.rowid JOIN documents d ON d.id=c.doc_id
        WHERE ranked.document_rank<=2
        ORDER BY ranked.rank,c.doc_id,c.ordinal LIMIT ?""",(match,*excluded,*([collection] if collection is not None else []),limit)).fetchall()
    out = []
    for row in rows:
        meta = json.loads(row["metadata"])
        matched = sorted(set(tokens)&set(_index_tokens(meta["title"]+" "+" ".join(meta["tags"])+" "+row["text"])))
        out.append({"id":meta.get("legacy_id",meta["id"]),"doc_id":meta["id"],"chunk_id":row["chunk_id"],
            "title":meta["title"],"category":meta["category"],"sources":meta["sources"],"event_date":meta["event_date"],
            "tags":meta["tags"],"text":row["text"],"start_char":row["start_char"],"end_char":row["end_char"],
            "always_include":meta["always_include"],"matched_terms":matched,"score":round(-row["rank"],8),
            **{key:meta[key] for key in _PROVENANCE_FIELDS if key in meta}})
    return out


def search(root: Path, query: str, limit: int = 3, *, collection=None) -> list[dict]:
    """Bounded management search, optionally filtered to one virtual folder."""
    _validate_query(query,limit)
    if not _tokens(query):
        return []
    store = _store(root)
    with store.locked(),store.db() as db:
        store.sync(db)
        return _search(db,query,limit,collection=collection)


def for_prompt(root: Path, query: str, limit: int = 3, *, expand_recent=False) -> list[dict]:
    """All explicitly enabled full texts plus other documents' retrieved chunks.

    Pinned full texts cannot be trimmed; recent activity is explicitly excerpted.
    Local expansion may retrieve other paragraphs of a recent post. All sources
    share the 50-reference trace bound and exact raw-source offsets.
    """
    _validate_query(query,limit,30)
    store = _store(root)
    with store.locked(),store.db() as db:
        store.sync(db)
        records = [json.loads(row[0]) for row in db.execute("SELECT metadata FROM documents ORDER BY id")]
        included = [record for record in records if record["always_include"]]
        if len(included) > MAX_ALWAYS_DOCUMENTS:
            raise ValueError("knowledge_always_include_limit")
        full = []
        for record in included:
            if record["category"] == "unverified":
                raise ValueError("knowledge_always_include_unverified")
            text = store.text(record)
            digest = _hash(text.encode())
            if digest != record["content_hash"]:
                raise ValueError("knowledge_source_hash_mismatch")
            full.append({"id":record.get("legacy_id",record["id"]),"doc_id":record["id"],
                "chunk_id":f"{record['id']}:full:{digest[:20]}","title":record["title"],
                "category":record["category"],"sources":record["sources"],"event_date":record["event_date"],
                "tags":record["tags"],"text":text,"start_char":0,"end_char":len(text),
                "always_include":True,"matched_terms":[],"score":0.0,
                **{key:record[key] for key in _PROVENANCE_FIELDS if key in record}})
        from .role_activity import config
        activity=config(root);keys=set(activity['sources']) if activity['enabled'] else set()
        from .context.recent import POST_LIMIT, VIDEO_LIMIT
        recent=[]; post_count=video_count=0; now=time.time()
        for record in sorted(records,key=lambda r:r.get('context_until',0),reverse=True):
            if (record.get('activity_source') not in keys or record.get('context_until',0)<=now or record.get('context_until',0)>now+86400
                or record['always_include'] or record['category']=='unverified'):continue
            is_video=record.get('activity_source','').startswith('bilibili_video:')
            if (is_video and video_count>=VIDEO_LIMIT) or (not is_video and post_count>=POST_LIMIT):continue
            if is_video:video_count+=1
            else:post_count+=1
            raw=store.text(record)
            # Contiguous source prefix keeps deletion/correction-aware trace references.
            text='\n'.join(raw.split('\n')[:4]) if is_video else raw[:1200]
            recent.append({**{k:record[k] for k in ('title','category','sources','tags','event_date','context_until','activity_source')},
                'doc_id':record['id'],'chunk_id':f"{record['id']}:recent:{_hash(text.encode())[:20]}",
                'text':text,'start_char':0,'end_char':len(text),'score':0.,'matched_terms':[],
                'selection_reason':'recent_activity','excerpted':len(raw)>len(text),'always_include':False})
            if len(full)+len(recent)>=50:break
        excluded=full if expand_recent else full+recent
        return full+recent+_search(db,query,min(limit,50-len(full)-len(recent)),[r['doc_id'] for r in excluded],for_prompt=True)
