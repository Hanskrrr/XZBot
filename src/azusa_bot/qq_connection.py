"""Fresh local QQ and reverse-WebSocket status; no chat text or login retries."""
import asyncio
import json
import sqlite3
from contextlib import closing
import time

from .napcat_admin import NapCatAdmin, NapCatError
from .settings import read_settings, private_write

LABELS = {
    'online':'QQ 接口报告在线，消息通道已连接；这不保证腾讯端实际收发正常', 'qq_offline':'QQ 已离线',
    'delivery_degraded':'QQ 接口报告在线，但最近连续发送未取得可靠回执；消息可能已经送达，请在 QQ 核对。此状态不等于已离线或被风控',
    'login_required':'QQ 需要登录', 'login_initializing':'QQ 登录正在初始化',
    'reconnecting':'QQ 正在重连', 'status_incomplete':'QQ 状态无法确认',
    'napcat_unavailable':'暂时无法读取 QQ 环境状态', 'bridge_stale':'消息通道状态已过期，无法确认在线',
    'bridge_disconnected':'QQ 消息通道已断开', 'bridge_status_unknown':'QQ 未返回有效的在线状态',
    'account_mismatch':'登录账号与本账号绑定不一致', 'not_bound':'尚未绑定 QQ',
    'not_applicable':'当前未使用普通 QQ 接入', 'status_stale':'QQ 登录状态已过期',
    'napcat_webui_rate_limited':'本机管理接口认证限流，暂时不能确认 QQ 状态；这不代表 QQ 被踢下线',
    'napcat_auth_failed':'本机管理接口认证失败，暂时不能确认 QQ 状态',
    'napcat_invalid_response':'本机管理接口返回异常，暂时不能确认 QQ 状态',
    'napcat_not_prepared':'QQ 管理凭据尚未准备',
    'napcat_probe_timeout':'本机状态读取超时，暂时不能确认 QQ 状态',
}
LOGIN_ERRORS={'session_expired':'QQ 提示登录态失效','account_restricted':'QQ 提示账号风险或限制','login_error':'QQ 返回了登录错误'}


class QQConnection:
    def __init__(self, root, *, clock=time.time, monotonic=time.monotonic):
        self.root, self.clock, self.monotonic = root, clock, monotonic
        self.lock = asyncio.Lock()
        self.login = None
        self.login_at = None
        self.bridge = None
        self.bridge_at = None
        self.previous = None
        self.path = root/'data/qq-connection-events.json'
        self.admin = NapCatAdmin(root)
        self.events = []
        self.traffic_cache = None
        self.traffic_at = None
        if self.path.is_file() and not self.path.is_symlink() and self.path.stat().st_size < 65536:
            try:
                self.events = [row for row in json.loads(self.path.read_text()) if isinstance(row,dict)
                               and row.get('reason_code') in LABELS and type(row.get('at')) in (int,float)][-40:]
            except (ValueError,TypeError):
                pass

    def traffic(self, self_id):
        """Read receipt metadata only; quiet groups never count as offline.

        A fresh heartbeat cannot erase failed deliveries. Only a later actual
        receipt clears them. No probe message or automatic relogin is sent.
        """
        now = self.clock()
        if (self.traffic_cache and self.traffic_cache['self_id']==self_id
                and self.traffic_at is not None and 0<=now-self.traffic_at<5):
            return self.traffic_cache['data']
        rows=[];received=None
        path=self.root/'data/state.sqlite3'
        if path.is_file() and not path.is_symlink():
            try:
                with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
                    scope='onebot:'+self_id+':%'
                    received=db.execute("SELECT MAX(at) FROM deliveries WHERE scope LIKE ? AND status NOT LIKE 'demo%'",(scope,)).fetchone()[0]
                    rows.extend(db.execute("SELECT status,at FROM deliveries WHERE scope LIKE ? AND status IN ('sent','uncertain','partial_uncertain') ORDER BY at DESC LIMIT 100",(scope,)))
                    rows.extend(db.execute("SELECT status,at FROM group_idle_attempts WHERE scope LIKE ? AND status IN ('sent','uncertain') ORDER BY at DESC LIMIT 100",(scope,)))
            except sqlite3.Error:
                pass
        path=self.root/'data/bilibili.sqlite3'
        if path.is_file() and not path.is_symlink():
            try:
                with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
                    rows.extend(db.execute("SELECT status,completed_at FROM outbox WHERE self_id=? AND status IN ('sent','uncertain') ORDER BY completed_at DESC LIMIT 100",(self_id,)))
            except sqlite3.Error:
                pass
        rows=sorted(((s,t) for s,t in rows if isinstance(t,(int,float)) and t<=now),key=lambda row:row[1],reverse=True)
        failures=0
        for status,at in rows:
            if status=='sent':break
            failures+=1
        data={'last_received_at':received,
              'last_successful_request_at':next((at for s,at in rows if s=='sent'),None),
              'last_uncertain_request_at':next((at for s,at in rows if s!='sent'),None),
              'consecutive_unconfirmed':failures}
        self.traffic_cache={'self_id':self_id,'data':data};self.traffic_at=now
        return data

    def report(self, self_id, connected, online):
        if self_id != read_settings(self.root).onebot_self_id:
            raise ValueError('qq_status_account_mismatch')
        if type(connected) is not bool or online is not None and type(online) is not bool or not connected and online is not None:
            raise ValueError('qq_status_invalid')
        self.bridge = {'self_id':self_id,'connected':connected,'online':online,'checked_at':self.clock()}
        self.bridge_at = self.monotonic()

    async def check(self):
        settings = read_settings(self.root)
        if settings.transport!='onebot' or not settings.onebot_self_id:
            return self.snapshot()
        async with self.lock:
            wall_age = self.clock()-(self.login or {}).get('checked_at',0)
            if self.login_at is None or self.monotonic()-self.login_at >= 10 or not 0<=wall_age<10:
                try:
                    self.login = await asyncio.wait_for(self.admin.status(),timeout=6)
                except NapCatError as exc:
                    reason=str(exc) if str(exc) in LABELS else 'napcat_auth_failed' if str(exc)=='napcat_webui_unauthorized' else 'napcat_unavailable'
                    self.login = {'state':'unavailable','reason_code':reason,'checked_at':self.clock()}
                except asyncio.TimeoutError:
                    self.login = {'state':'unavailable','reason_code':'napcat_probe_timeout','checked_at':self.clock()}
                self.login_at = self.monotonic()
        return self.snapshot()

    def snapshot(self):
        settings = read_settings(self.root)
        login = self.login or {}
        bridge = self.bridge if self.bridge and self.bridge['self_id']==settings.onebot_self_id else None
        fresh_login = (self.login_at is not None and self.monotonic()-self.login_at <= 30
                       and 0<=self.clock()-login.get('checked_at',0)<=30)
        fresh_bridge = (bridge is not None and self.monotonic()-self.bridge_at <= 45
                        and 0<=self.clock()-bridge['checked_at']<=45)
        state,reason = 'unknown','status_incomplete'
        if settings.transport!='onebot':state,reason='not_applicable','not_applicable'
        elif not settings.onebot_self_id:state,reason='not_bound','not_bound'
        elif not fresh_login:state,reason='unknown','status_stale'
        elif login.get('state')!='online':state,reason=login.get('state','unknown'),login.get('reason_code','status_incomplete')
        elif login.get('qq_number')!=settings.onebot_self_id:state,reason='mismatch','account_mismatch'
        elif not fresh_bridge:reason='bridge_stale'
        elif not bridge['connected']:state,reason='disconnected','bridge_disconnected'
        elif bridge['online'] is False:state,reason='offline','qq_offline'
        elif bridge['online'] is not True:reason='bridge_status_unknown'
        else:state,reason='online','online'
        traffic = self.traffic(settings.onebot_self_id) if settings.transport=='onebot' and settings.onebot_self_id else {}
        if state=='online' and traffic.get('consecutive_unconfirmed',0)>=2:
            state,reason='degraded','delivery_degraded'
        now = self.clock()
        login_error=login.get('login_error_code') if login.get('login_error_code') in LOGIN_ERRORS else None
        key = (settings.onebot_self_id,state,reason,login_error)
        if key!=self.previous:
            self.previous=key
            self.events.append({'at':now,'state':state,'reason_code':reason,
                                **({'login_error_code':login_error} if login_error else {})})
            self.events=self.events[-40:]
            private_write(self.path,json.dumps(self.events,ensure_ascii=False))
        return {'state':state,'online':state=='online','reason_code':reason,'message':LABELS.get(reason,LABELS['status_incomplete']),
                'traffic':traffic,
                'checked_at':now,'fresh_until':now+30,'login_checked_at':login.get('checked_at'),
                'bridge_checked_at':bridge.get('checked_at') if bridge else None,
                'login_error_code':login_error,
                'events':[{**row,'message':LABELS[row['reason_code']]+(' · '+LOGIN_ERRORS[row['login_error_code']] if row.get('login_error_code') in LOGIN_ERRORS else '')} for row in reversed(self.events)]}

    async def run(self):
        while True:
            try:
                await self.check()
            except Exception:
                # A monitoring failure must not initiate a login or change reply settings.
                self.login_at=None
            await asyncio.sleep(15)
