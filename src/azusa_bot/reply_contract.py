"""Per-call output requirements, separate from character prose and chat evidence.

The chat API's JSON mode guarantees syntax, not required application fields.
This contract is prompt-visible and traceable; local gates remain authoritative.
"""
import json

from . import participation_vote

CHAT_KINDS = frozenset({'reply', 'group_decision', 'followup', 'group_idle', 'group_idle_manual'})


def build(settings, kind, *, tools=(), idle_plan=False):
    score = {'type':'number', 'minimum':0, 'maximum':1}
    if kind == 'group_plan':
        ids = {'type':'array','items':{'type':'string'},'uniqueItems':True,'maxItems':20}
        anchor = {'type':'null' if idle_plan else 'string'}
        fields = {
            'action':{'enum':['reply','observe','wait']},
            'target':{'enum':['group'] if idle_plan else ['self','others','group','unknown']},
            'addressee_ids':dict(ids, maxItems=0) if idle_plan else ids,
            'response_to_message_id':anchor,
            'thread_anchor':anchor,
            'evidence_message_ids':dict(ids, minItems=0 if idle_plan else 1),
            'reason':{'type':'string','maxLength':64,'pattern':'^[a-z][a-z0-9_]{0,63}$'},
        }
        properties = {'participation_plan':{'type':'object','required':list(fields),'properties':fields}}
        if settings.group_vote_enabled:
            properties['votes'] = {'type':'object','required':list(participation_vote.CRITERIA),
                'properties':{key:dict(score) for key in participation_vote.CRITERIA}}
        else:
            properties['confidence'] = score
        return {'type':'object','required':list(properties),'properties':properties}
    if kind not in CHAT_KINDS:
        return None
    modes = ['text'] if kind in {'group_idle','group_idle_manual'} else ['text', 'image']
    if settings.voice_enabled and settings.voice_authorized:
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
    if kind not in {'group_idle','group_idle_manual'}:
        for key in tools:
            properties[key] = {'type':'object'} if key == 'web_search' else {'type':'string'}
        properties.update(asset_id={'type':'string'}, citations={'type':'array','items':{'type':'integer'}})
    return {'type':'object', 'required':required, 'properties':properties}


def planning_instructions(contract, current_message_id):
    """Show the complete JSON format and distinguish common live routing cases."""
    idle = current_message_id is None
    example = {'participation_plan':{
        'action':'observe', 'target':'group' if idle else 'unknown', 'addressee_ids':[],
        'response_to_message_id':current_message_id, 'thread_anchor':current_message_id,
        'evidence_message_ids':[] if idle else [current_message_id],
        'reason':'no_new_contribution' if idle else 'unclear_addressee',
    }}
    if 'votes' in contract['required']:
        example['votes'] = dict.fromkeys(participation_vote.CRITERIA, .2)
    else:
        example['confidence'] = .2
    instructions = (
        '只输出一个完整的 JSON 对象。participation_plan 和本轮评分是同一个对象中的顶层字段，'
        '始终给齐 required 列出的字段。省略聊天正文、发送方式和 Markdown。\n'
    )
    if idle:
        instructions += '以下示例展示冷场时选择观察的 JSON 格式，实际判断与评分以本轮消息为准。\n'
    else:
        instructions += (
            'action 和 target 分开判断。以下两例仅展示动作和受话人字段。尖括号内是占位符，填值时使用本轮核实的账号 ID。\n'
            '甲引用乙的照片并 @乙，问“怎么天天吃这个”，当前角色继续观察。\n'
            '{"action":"observe","target":"others","addressee_ids":["<乙的账号ID>"]}\n'
            '群友明确叫当前角色回答问题，当前角色建议回应。\n'
            '{"action":"reply","target":"self","addressee_ids":["<本轮self_id>"]}\n'
            '下面是无法确定受话人、也找不到可靠前文关联时的完整 JSON 示例。实际判断与评分以本轮消息为准。\n'
        )
    return instructions + json.dumps(example, ensure_ascii=False) + '\n本轮规划字段约定如下。'


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
