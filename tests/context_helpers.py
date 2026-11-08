"""Inspect actual provider envelopes without assuming canonical internal indexes."""
import json


def context(messages):
    result = {}
    for message in messages[1:]:
        content = message['content']
        texts = [content] if isinstance(content, str) else [p['text'] for p in content if p.get('type') == 'text']
        for text in texts:
            try:
                data = json.loads(text.split('\n', 1)[1])
            except (IndexError, ValueError):
                continue
            if isinstance(data, dict):
                result.update(data)
    return result


def dialogue(messages):
    return [row for row in context(messages).get('recent_conversation', []) if row.get('in_direct_dialogue')]
