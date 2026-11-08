"""Bounded receipt diagnostics; never persist upstream wording or payloads."""
import re

_MESSAGES = {
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
    result = getattr(error, 'result', {})
    result = result if isinstance(result, dict) else {}
    retcode = result.get('retcode')
    # Inspect wording only to select fixed categories. It never leaves here.
    details = str(result.get('message', '')) + str(result.get('wording', '')) + str(error)
    if '1006514' in details:
        return 'session_invalid'
    if isinstance(error, TimeoutError) or 'timeout' in details.lower() or '超时' in details:
        return 'send_timeout'
    if isinstance(error, ValueError) and str(error) in {'Missing idle receipt', 'Invalid message identity'}:
        return 'missing_receipt'
    if type(retcode) is int and abs(retcode) < 2**31:
        return 'onebot_retcode_' + str(retcode)
    return 'api_unavailable' if type(error).__name__ == 'ApiNotAvailable' else 'bridge_error'
