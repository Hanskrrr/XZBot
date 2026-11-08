"""Read-only, authenticated OneBot health. WebUI uptime is not QQ login proof."""
import re
import time

import httpx

from .napcat_admin import NapCatAdmin, NapCatError
from .runtime_layout import runtime_layout
from .settings import token


class SnowLumaAdmin(NapCatAdmin):
    async def _onebot(self, action):
        if action not in {'get_status', 'get_login_info'}:
            raise NapCatError('snowluma_invalid_response')
        port = runtime_layout(self.root).core_port + 6
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=3, follow_redirects=False) as client:
                response = await client.post(f'http://127.0.0.1:{port}/{action}', json={},
                    headers={'Authorization': 'Bearer ' + token(self.root, 'onebot')})
            if response.status_code == 401:
                raise NapCatError('snowluma_auth_failed')
            if response.status_code != 200 or len(response.content) > 65536:
                raise NapCatError('snowluma_invalid_response')
            data = response.json()
            if (not isinstance(data, dict) or data.get('status') != 'ok'
                    or type(data.get('retcode')) is not int or data['retcode'] != 0
                    or not isinstance(data.get('data'), dict)):
                raise NapCatError('snowluma_invalid_response')
            return data['data']
        except (httpx.HTTPError, ValueError):
            raise NapCatError('snowluma_unavailable') from None

    async def status(self):
        health = await self._onebot('get_status')
        info = await self._onebot('get_login_info')
        number = str(info.get('user_id', ''))
        if not re.fullmatch(r'[1-9][0-9]{0,19}', number):
            raise NapCatError('snowluma_invalid_response')
        online = health.get('online') is True and health.get('good') is True
        offline = health.get('online') is False
        return {'available': True, 'logged_in': online, 'offline': offline,
                'core_ready': health.get('good') is True, 'qq_number': number,
                'nickname': str(info.get('nickname', ''))[:100], 'checked_at': time.time(),
                'state': 'online' if online else 'offline' if offline else 'unknown',
                'reason_code': 'online' if online else 'qq_offline' if offline else 'status_incomplete'}

    async def qr_svg(self):
        raise NapCatError('snowluma_desktop_login_required')

    async def refresh_qr_svg(self):
        raise NapCatError('snowluma_desktop_login_required')
