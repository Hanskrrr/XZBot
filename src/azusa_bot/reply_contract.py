"""Per-call output requirements, separate from character prose and chat evidence.

The chat API's JSON mode guarantees syntax, not required application fields.
This contract is prompt-visible and traceable; local gates remain authoritative.
"""
from . import participation_vote

CHAT_KINDS = frozenset({'reply', 'group_decision', 'followup', 'group_idle'})


def build(settings, kind, *, tools=()):
    if kind not in CHAT_KINDS:
        return None
    score = {'type':'number', 'minimum':0, 'maximum':1}
    modes = ['text'] if kind == 'group_idle' else ['text', 'image']
    if kind != 'group_idle' and settings.voice_enabled and settings.voice_authorized:
        modes.append('voice')
    properties = {'text':{'type':'string'}, 'mode':{'enum':modes}}
    required = ['text', 'mode']
    if participation_vote.enabled(settings, kind):
        properties['votes'] = {'type':'object', 'required':list(participation_vote.CRITERIA),
            'properties':{key:dict(score) for key in participation_vote.CRITERIA}}
        required.append('votes')
    elif kind in participation_vote.KINDS:
        properties.update(participate={'type':'boolean'}, confidence=dict(score))
        required += ['participate', 'confidence']
    if kind == 'followup':
        properties['continuation'] = {'type':'boolean'}
        required.append('continuation')
    if kind != 'group_idle':
        for key in tools:
            properties[key] = {'type':'object'} if key == 'web_search' else {'type':'string'}
        properties.update(asset_id={'type':'string'}, citations={'type':'array','items':{'type':'integer'}})
    return {'type':'object', 'required':required, 'properties':properties}


def control_errors(answer, settings, kind):
    """Never invent scores or repair them from a single confidence value."""
    errors = []
    if participation_vote.enabled(settings, kind):
        raw = answer.get('votes')
        raw = raw if isinstance(raw, dict) else {}
        errors += ['votes.'+key for key in participation_vote.CRITERIA if not participation_vote.numeric(raw.get(key))]
    elif kind in participation_vote.KINDS:
        if type(answer.get('participate')) is not bool:errors.append('participate')
        if not participation_vote.numeric(answer.get('confidence')):errors.append('confidence')
    if kind == 'followup' and type(answer.get('continuation')) is not bool:
        errors.append('continuation')
    return errors
