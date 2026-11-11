"""A small speaker background plus relevant saved memories, never raw replay.

The store enforces profile/scope/subject boundaries before selection. This is
lexical retrieval, not semantic search or a newly inferred personality profile.
No extra model call, persistent copy, or locally running inference model.
"""
from __future__ import annotations

import re
from . import member_notes


FACETS = {"identity", "preference", "experience", "ongoing", "general"}
SELECTIONS = {"member_background", "recent_interaction", "related_member_background", "related_member_interaction", "current_message", "recent_topic"}


def _topic_query(query, recent, user):
    """Resolve short/deictic questions only from the already eligible timeline."""
    if len(query.strip()) > 16 and not re.search(r"它|那个|这个|之前|上次|刚才|接着|后来|还记得|又|继续",query):
        return ""
    texts=[]
    # Prefer the speaker's own recent words to another member's side discussion.
    for same_speaker in (True,False):
        for row in reversed(recent[-12:]):
            if not isinstance(row,dict) or row.get("role")=="assistant":continue
            age=row.get("age_seconds")
            if type(age) not in (int,float) or not 0<=age<=180:continue
            if (row.get("user_id")==user)!=same_speaker:continue
            text=row.get("text","")
            if not isinstance(text,str) or not text.strip() or text==query or text in texts:continue
            texts.append(text[:240])
            if len(texts)==4:return "\n".join(texts)
    return "\n".join(texts)


def select(store, scope, user, query, *, recent=(), related_users=(), limit=12, max_chars=3600):
    """Return real memory rows annotated with why they enter this request.

    Up to three bounded own facts identify the speaker even for a greeting.
    Current-query matches have priority over recent-topic matches. Whole facts
    are kept or omitted: cutting a sentence could remove a negation or date.
    Familiarity/nickname remains supplied by the authenticated event pipeline.
    """
    store._key(scope);store._key(user);store._limit(limit,32)
    if not isinstance(query,str) or len(query)>32000:raise ValueError("Invalid retrieval query")
    if type(max_chars) is not int or not 1<=max_chars<=8000:raise ValueError("Invalid memory context budget")
    with store._lock:
        own=store.list_memories(scope,user,subject=user)
        primary=store.retrieve(scope,user,query,limit=limit)
        topic=_topic_query(query,list(recent),user)
        secondary=store.retrieve(scope,user,topic,limit=limit) if topic else []
        selected=[];ids=set();used=0

        def take(row,reason,cap):
            nonlocal used
            if (row['id'] in ids or len(selected)>=limit or len(row['text'])>cap
                    or used+len(row['text'])>max_chars):return False
            selected.append(dict(row,selection_reason=reason))
            ids.add(row['id']);used+=len(row['text']);return True

        intro=next((r for r in own if r.get('note_type')=='background'),None)
        if intro:take(intro,'member_background',member_notes.BACKGROUND_LIMIT)
        # Until a first introduction is organized, keep known factual fragments
        # available without labelling them as a completed introduction.
        own=[r for r in own if not r.get('note_type')]
        # Stable facts are preferred; legacy/unclassified facts are labelled as
        # background fragments, never silently reclassified as identity facts.
        order={"identity":0,"preference":1,"general":2,"ongoing":3,"experience":4}
        own.sort(key=lambda r:(order.get(r.get('facet'),2),r['source']!='explicit',-r['updated_at'],-r['id']))
        for row in ([] if intro else own):
            if len(selected)>=min(3,limit) or used>=900:break
            take(row,"member_background",min(400,900-used))
        for row in member_notes.recent(store,scope,user,limit=3):
            take(row,'recent_interaction',member_notes.INTERACTION_LIMIT)
        # Only explicitly involved members from this same group. Never load
        # another member's ordinary personal fact archive or private messages.
        if ':group:' in scope:
            for subject in list(dict.fromkeys(related_users))[:1]:
                if subject==user or not store._consent(scope,subject):continue
                intro=member_notes.background(store,scope,subject)
                if intro:take(intro,'related_member_background',member_notes.BACKGROUND_LIMIT)
                for row in member_notes.recent(store,scope,subject,limit=1):
                    take(row,'related_member_interaction',member_notes.INTERACTION_LIMIT)
        for row in primary:take(row,"current_message",1200)
        topic_count=0
        for row in secondary:
            if topic_count==2:break
            topic_count+=take(row,"recent_topic",700)
        # A matching anchor keeps both roles visible without copying its text.
        direct_ids={r['id'] for r in primary}
        for row in selected:
            row['matches_current_message']=row['id'] in direct_ids
        return selected


def partition(rows):
    """Provider-only layout; references and trimming retain the original rows."""
    members={};extra=[]
    for row in rows:
        kind=row.get('note_type');reason=row.get('selection_reason')
        if kind not in {'background','interaction'} or reason not in {
                'member_background','recent_interaction','related_member_background','related_member_interaction'}:
            extra.append(row);continue
        member=members.setdefault(row['subject'],{'user_id':row['subject'],'background':None,'recent_interactions':[]})
        if kind=='background':member['background']=row
        else:member['recent_interactions'].append(row)
    return list(members.values()),extra
