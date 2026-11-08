"""Bounded public-web evidence using DeepSeek's documented Search API.

Only a short public query reaches the search endpoint. No chat transcript,
persona, memory dump, arbitrary URL fetching, or separate search credential.
"""
from __future__ import annotations

from .prompts import DEFAULTS as PROMPT_DEFAULTS, prompt

from copy import deepcopy
import ipaddress
import json
import re
import time
from urllib.parse import urlsplit

import httpx

from .group_chat import GroupConversation
from .providers import ProviderError

POLICY = PROMPT_DEFAULTS["search"]

OFFLINE_POLICY = PROMPT_DEFAULTS["search_offline"]


def public_query(value):
    if not isinstance(value,str) or not 1<=len(value.strip())<=128:
        raise ProviderError("web_search_query_invalid")
    value=value.strip()
    if (GroupConversation.contains_credentials(value) or re.search(r"\d{5,}|[\w.+-]+@[\w.-]+\.[a-z]{2,}|https?://|[\r\n]",value,re.I)):
        raise ProviderError("web_search_query_private")
    return value


def public_url(value):
    if not isinstance(value,str) or len(value)>2000 or any(ord(c)<33 for c in value):
        return ""
    try:
        url=urlsplit(value)
        host=url.hostname or ""
        if (url.scheme not in {"http","https"} or url.username or url.password or not host
                or "." not in host or host.endswith((".local",".localhost",".internal"))):
            return ""
        try:
            if not ipaddress.ip_address(host).is_global:
                return ""
        except ValueError:
            pass
        return value
    except ValueError:
        return ""


async def search(query, key, *, transport=None):
    query=public_query(query)
    if not key:
        raise ProviderError("deepseek_not_configured")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15,connect=5),follow_redirects=False,transport=transport) as client:
            async with client.stream("POST","https://api.deepseek.com/v1/tools/search",
                    headers={"Authorization":"Bearer "+key},
                    json={"query":query,"count":3,"need_content":True}) as response:
                if response.status_code!=200:
                    raise ProviderError("web_search_http_"+str(response.status_code))
                raw=bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw)>1024*1024:
                        raise ProviderError("web_search_response_too_large")
        body=json.loads(raw)
        if not isinstance(body,dict) or not isinstance(body.get("results"),list):
            raise ValueError()
        results=[]
        for row in body["results"][:3]:
            if not isinstance(row,dict) or not (url:=public_url(row.get("url"))):
                continue
            content=row.get("content") or row.get("summary") or row.get("snippet")
            if not isinstance(content,str) or not content.strip():
                continue
            results.append({"id":len(results)+1,"url":url,"title":str(row.get("title") or "来源")[:160],
                "published_at":str(row.get("date") or "")[:50],"content":content[:1600],
                "content_kind":"page_excerpt" if row.get("content") else "search_summary"})
        return {"status":"success" if results else "no_results","query":query,"searched_at":time.time(),"results":results}
    except ProviderError:
        raise
    except (httpx.HTTPError,ValueError,TypeError):
        raise ProviderError("web_search_unavailable") from None


def provenance(evidence):
    """Persist source identifiers, never a copy of page content or raw errors."""
    return {key:evidence[key] for key in ("status","query","searched_at") if key in evidence} | {
        "sources":[{key:row[key] for key in ("id","title","url","published_at","content_kind") if key in row}
                   for row in evidence.get("results",[])[:3]]}


def with_evidence(messages,evidence,*,final_policy=None,prefix=None):
    result=deepcopy(messages)
    final_policy = PROMPT_DEFAULTS["search_done"] if final_policy is None else final_policy
    prefix = prefix if prefix is not None else result[1]["content"].split("\n",1)[0]+"\n"
    data=result[1]["content"][len(prefix):]
    data=json.loads(data);data["web_evidence"]=evidence
    result[1]["content"]=prefix+json.dumps(data,ensure_ascii=False)
    result[0]["content"] += final_policy
    return result


def wants_sources(text):
    if not isinstance(text,str): return False
    if re.search(r"(?:不用|不要|不必|无需|别|不需要).{0,10}(?:来源|出处|链接|引用|参考|证据)|(?:no|without|don.t).{0,15}(?:sources?|citations?|links?|references?)",text,re.I):
        return False
    return bool(re.search(r"(?:来源|出处|原文|链接|证据|参考文献)|\b(?:sources?|citations?|references?|links?)\b",text,re.I))


def cite(answer,evidence,*,show=False):
    if not show:
        return answer
    text=answer.get("text")
    if not isinstance(text,str) or not text.strip() or not evidence.get("results"):
        return answer
    selected=answer.get("citations",[])
    if not isinstance(selected,list):
        selected=[]
    rows=[row for row in evidence["results"] if any(type(n) is int and n==row["id"] for n in selected)]
    # A searched answer is inspectable even when a model omits its citation IDs.
    if not rows:
        rows=evidence["results"][:2]
    answer=dict(answer)
    answer["text"]=text.rstrip()+"\n\n参考来源：\n"+"\n".join(f'{row["title"]} {row["url"]}' for row in rows)
    answer["mode"]="text"
    return answer
