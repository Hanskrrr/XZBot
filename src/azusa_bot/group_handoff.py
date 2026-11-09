"""Local candidate selection, never a claim about who a pronoun refers to."""
import re
from .dialogue import refers_back


def candidate(context, text, settings, *, require_cue=True):
    if (not settings.group_followup_enabled or not settings.group_followup_handoff_enabled
            or (require_cue and not (re.search(r'[你您妳]', text[:600]) or refers_back(text)))):
        return None
    for index in range(len(context)-1, -1, -1):
        row = context[index]
        if row['role'] != 'assistant':
            continue
        age = row.get('age_seconds', 0)
        following = sum(item['role'] == 'user' for item in context[index+1:])
        if age > settings.group_followup_handoff_seconds or following > settings.group_followup_handoff_messages:
            return None
        return {'source': 'recent_group_reply', 'anchor_message_id': row['message_id'],
                'anchor_age_seconds': age, 'following_messages': following}
    return None
