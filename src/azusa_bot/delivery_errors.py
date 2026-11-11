"""Bounded receipt diagnostics; never persist upstream wording or payloads."""
import re

CONVERSATION_CODES = frozenset({'conversation_plan_changed','conversation_plan_invalid',
    'conversation_plan_unavailable','conversation_observed','peer_round_limit'})

_MESSAGES = {
    'conversation_plan_changed': '本角色看到的对话关系已变化，旧回复已取消。',
    'conversation_plan_invalid': '本轮对话规划缺少有效字段或可核实的消息依据，未生成回复。',
    'conversation_plan_unavailable': '本轮对话规划未能完成，已保持安静。',
    'conversation_observed': '本角色判断此时无需回应，已保持旁听。',
    'peer_round_limit': '本角色与其他机器人的连续互动已达到本地轮数限制，已停止接话。',
    'bridge_policy_changed': '发送前账号、白名单或群聊开关已变化，没有向 QQ 发出发送请求。',
    'send_authorization_denied': '发送前授权已失效（角色切换、上下文或发送时限变化），没有向 QQ 发出发送请求。',
    'send_not_attempted': '准备发送时中止，没有向 QQ 发出发送请求。',
    'session_invalid': 'QQ 返回会话失效；请检查登录状态。',
    'send_timeout': '等待 QQ 发送回执超时；消息仍可能已经送达。',
    'missing_receipt': 'QQ 接口未返回有效消息编号；消息仍可能已经送达。',
    'api_unavailable': '发送时 QQ 接口连接不可用。',
    'bridge_error': '发送或解析回执时出现异常，无法确认送达。',
}


def safe_delivery_code(value):
    if not isinstance(value, str):
        return 'bridge_error'
    if value in _MESSAGES or re.fullmatch(r'onebot_retcode_-?[0-9]{1,10}', value):
        return value
    return 'bridge_error'


def delivery_explanation(code):
    code = safe_delivery_code(code)
    return _MESSAGES.get(code, 'QQ 接口返回错误码 ' + code.removeprefix('onebot_retcode_') + '；不能仅凭此码判断是否被风控或实际送达。')


def classify_send_error(error):
    if str(error) in CONVERSATION_CODES:
        return str(error)
    result = getattr(error, 'result', {})
    result = result if isinstance(result, dict) else {}
    retcode = result.get('retcode')
    # Inspect wording only to select fixed categories. It never leaves here.
    details = str(result.get('message', '')) + str(result.get('wording', '')) + str(error)
    if '1006514' in details:
        return 'session_invalid'
    if isinstance(error, TimeoutError) or 'timeout' in details.lower() or '超时' in details:
        return 'send_timeout'
    if isinstance(error, ValueError) and str(error) in {'Missing idle receipt', 'Invalid message identity', 'OneBot delivery receipt unavailable','Missing segment receipt'}:
        return 'missing_receipt'
    if type(retcode) is int and abs(retcode) < 2**31:
        return 'onebot_retcode_' + str(retcode)
    return 'api_unavailable' if type(error).__name__ == 'ApiNotAvailable' else 'bridge_error'
