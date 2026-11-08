"""Content-free operational diagnostics, visible only to local administrators."""
from __future__ import annotations
from contextlib import contextmanager
from pathlib import Path
import re,sqlite3,time

_CODES = {'output_contract_invalid','ok','unknown','internal_failure','credential_store_unavailable','deepseek_not_configured',
 'model_not_configured','deepseek_incomplete','deepseek_network_or_format','invalid_reply_object',
 'empty_reply','fish_not_configured','fish_audio_too_large','fish_invalid_wav','fish_network_or_storage',
 'voice_budget_exhausted','cloud_budget','summary_invalid','provider_unknown_error',
 'gemini_balance_not_configured','gemini_balance_incomplete','gemini_balance_network_or_format',
 'context_length_exceeded','generation_length_limit','context_budget_exceeded',
 'privacy_context_changed','context_retry_budget','web_search_query_invalid','web_search_query_private',
 'web_search_response_too_large','web_search_unavailable','web_search_mock_disabled','web_search_budget',
 'web_search_credentials_unavailable','execution_budget_exhausted','execution_timeout'}

CANCELLATION_REASONS = {
    'group_settings_changed':'这个群的参与设置已更新，旧回复已取消。',
    'persona_changed':'人设正文已更新，旧回复已取消。',
    'prompts_changed':'角色提示词已更新，旧回复已取消。',
    'conversation_settings_changed':'预设问答或对话设置已更新，旧回复已取消。',
    'provider_settings_changed':'模型或语音设置已更新，旧回复已取消。',
    'knowledge_changed':'角色背景资料已更新，旧回复已取消。',
    'role_activity_changed':'角色动态设置已更新，旧回复已取消。',
    'profile_changed':'角色已切换，原角色的旧回复已取消。',
    'profile_settings_changed':'角色档案已更新，旧回复已取消。',
    'memory_changed':'记忆或历史已更正或删除，旧回复已取消。',
    'privacy_changed':'记忆或旁听授权已更新，旧回复已取消。',
    'settings_changed':'运行配置已更新，旧回复已取消。',
    'context_cleared':'会话上下文已清空，旧回复已取消。',
    'source_changed':'请求所依据的上下文或来源已失效，旧回复已取消。',
}


def safe_code(value) -> str:
    value = str(value)
    return value if value in _CODES or re.fullmatch(r'(?:deepseek|fish|gemini_balance|web_search)_http_[1-5][0-9]{2}',value) or re.fullmatch(r'(?:deepseek|gemini_balance)_(?:timeout|network_error|response_not_json|response_shape_invalid|empty_content|output_not_json|output_not_object)',value) else 'provider_unknown_error'


def explanation(code):
    if code=='output_contract_invalid':return '模型返回缺失或无效的接话控制字段；本次没有通过输出校验。具体字段与本群生效设置见调用轨迹。'
    if code=='ok':return '最近一次调用成功。'
    if code=='unknown':return '尚无调用结果。'
    if re.fullmatch(r'(?:deepseek|gemini_balance)_timeout',code):return '等待模型服务响应超时；这不是 QQ 登录错误。'
    if code.endswith('_network_error'):return '连接模型服务失败或连接中断；本次未得到可解析的完整响应。'
    if code.endswith(('_response_not_json','_response_shape_invalid')):return '模型接口返回的响应封装不符合 Chat Completions 格式，请核对服务兼容性。'
    if code.endswith('_empty_content'):return '模型接口成功返回，但回答正文为空；没有发送占位消息。'
    if code.endswith(('_output_not_json','_output_not_object')):return '模型已返回内容，但没有遵守 JSON 对象输出格式；请检查额外提示词与预设问答的格式要求。这不是断网。'
    if code=='deepseek_network_or_format':return '旧版合并错误码：可能是网络失败，也可能是模型返回内容无法解析，不能仅据此判断断网；新版已分别记录。'
    if code=='execution_timeout':return '本轮生成与资料查询超过 180 秒，已停止；没有发送未完成的回答。'
    if code=='execution_budget_exhausted':return '本轮生成次数已达到对话设置中的上限，已停止继续调用。'
    if code=='context_length_exceeded':return '模型明确拒绝了过长上下文。请核对当前模型的上下文窗口配置，并缩短核心人设或输入；普通限流与繁忙不会归为这个错误。'
    if code=='generation_length_limit':return '模型因生成长度或总上下文限制停止，返回内容可能不完整；没有把截断内容发送到聊天。'
    if code=='context_budget_exceeded':return '核心人设、当前问题与回复预留已超过本地上下文预算；请把长背景移入背景资料库，或核对模型实际窗口后调整预算。没有擅自截断核心人设。'
    if code=='privacy_context_changed':return '配置、角色、记忆授权或会话上下文在处理期间更新，旧回复已主动取消。这不是 API 认证错误，也不表示隐私泄露。具体触发项见调用轨迹；旧记录可能未保存触发项。'
    if code=='context_retry_budget':return '本地每日云端调用额度不足以进行上下文缩减重试，本次未再请求模型。'
    if code=='gemini_balance_network_or_format':return 'Gemini 本机网关未启动、连接失败或返回格式不符合要求；先到 Gemini Balance 面板检查运行状态。'
    if code.endswith('_401'):return '服务拒绝了认证，请在模型与语音检查密钥来源和有效性。'
    if code.endswith('_402'):return '服务返回余额不足，请检查供应商账户。'
    if code.endswith('_429'):return '服务限流，请降低调用频率或检查账户限额。'
    if code.endswith(('_500','_502','_503','_504')):return '上游服务出错或繁忙；这不代表 QQ 登录有问题。稍后再试或更换已验证的服务。'
    if code.endswith(('_400','_404','_422')):return '服务拒绝模型名称、地址或请求参数，请检查所选模型的接口兼容性。'
    if 'not_configured' in code:return '尚未配置所需密钥、模型或音色，请到模型与语音补齐。'
    if code=='credential_store_unavailable':return '系统钥匙串暂不可访问，请检查系统授权。'
    if code in {'cloud_budget','voice_budget_exhausted'}:return '本地每日云端调用额度已用完，可在设置中调整。'
    if 'network' in code:return '连接失败或响应格式不符合要求；检查网络与模型兼容性。'
    if code in {'deepseek_incomplete','gemini_balance_incomplete','invalid_reply_object','empty_reply','summary_invalid'}:return '模型未返回完整且符合约定的内容；没有将错误提示发送到聊天。'
    return '处理未完成，技术详情仅保存在本机；聊天中不会发送故障占位句。'


class Diagnostics:
    def __init__(self,path: Path):
        self.path=path
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS diagnostic_services(
                service TEXT PRIMARY KEY,code TEXT,at REAL,failures INTEGER);
                CREATE TABLE IF NOT EXISTS diagnostic_events(
                id INTEGER PRIMARY KEY,service TEXT,code TEXT,at REAL);''')

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()

    def record(self,service,code):
        if service not in {'llm','tts','summary','core'}:raise ValueError('unknown_service')
        code=safe_code(code);now=time.time()
        with self.db() as db:
            old=db.execute('SELECT * FROM diagnostic_services WHERE service=?',(service,)).fetchone()
            failures=0 if code=='ok' else (old['failures'] if old else 0)+(code!='privacy_context_changed')
            db.execute('INSERT OR REPLACE INTO diagnostic_services VALUES(?,?,?,?)',(service,code,now,failures))
            if code!='ok' or not old or old['code']!='ok':
                db.execute('INSERT INTO diagnostic_events(service,code,at) VALUES(?,?,?)',(service,code,now))
                db.execute('DELETE FROM diagnostic_events WHERE id NOT IN (SELECT id FROM diagnostic_events ORDER BY id DESC LIMIT 100)')

    def status(self):
        with self.db() as db:
            found={r['service']:dict(r) for r in db.execute('SELECT * FROM diagnostic_services')}
            rows=[found.get(s,{'service':s,'code':'unknown','at':None,'failures':0}) for s in ('llm','tts','summary','core')]
            events=[dict(r) for r in db.execute('SELECT service,code,at FROM diagnostic_events ORDER BY id DESC LIMIT 30')]
        for row in rows+events:
            row['state']='unknown' if row['code']=='unknown' else 'ok' if row['code']=='ok' else 'cancelled' if row['code']=='privacy_context_changed' else 'error'
            row['message']=explanation(row['code'])
        return {'services':rows,'events':events}
