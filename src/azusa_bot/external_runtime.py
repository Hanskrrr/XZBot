"""Externally managed native NapCat; XZBot controls only its own bridge."""
import io
import json
import platform
import zipfile
from .settings import token
from .runtime_layout import runtime_layout


def legacy_enabled(root):
    marker=root/'config/runtime.json'
    if not marker.exists():return platform.system()!='Darwin'
    if marker.is_symlink() or marker.stat().st_size>1024:raise ValueError('runtime_config_invalid')
    data=json.loads(marker.read_text())
    if data not in ({'mode':'external'}, {'mode':'lima'}):raise ValueError('runtime_config_invalid')
    return data['mode']=='external'


def enabled(root):
    from .qq_runtime import read
    return read(root)['environment']=='external'


def connection_archive(root):
    from .qq_runtime import read
    layout=runtime_layout(root)
    choice=read(root)
    if choice['adapter']=='snowluma':
        host='host.lima.internal' if choice['environment']=='lima' else '127.0.0.1'
        files={'onebot.json':{'networks':{
            'httpServers':[{'name':'xzbot-status','enabled':True,'host':'127.0.0.1',
                'port':layout.core_port+6,'path':'/','accessToken':token(root,'onebot'),
                'messageFormat':'array','reportSelfMessage':False}],
            'httpClients':[], 'wsServers':[],
            'wsClients':[{'name':'xzbot-local','enabled':True,
                'url':f'ws://{host}:{layout.onebot_port}/ws','role':'Universal',
                'accessToken':token(root,'onebot'),'messageFormat':'array',
                'reportSelfMessage':False,'reconnectIntervalMs':5000}]}}}
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
            for name,data in files.items():archive.writestr(name,json.dumps(data,ensure_ascii=False,indent=2))
        return buffer.getvalue()
    host='host.lima.internal' if choice['environment']=='lima' else '127.0.0.1'
    files={
        'webui.json':{'host':'127.0.0.1','port':6099 if choice['environment']=='lima' else layout.napcat_port,'token':token(root,'napcat-webui'),
                      'autoLoginAccount':'','disableWebUI':False},
        'onebot11.json':{'network':{'websocketClients':[{'name':'xzbot-local','enable':True,
            'url':f'ws://{host}:{layout.onebot_port}/ws','token':token(root,'onebot'),
            'messagePostFormat':'array','reportSelfMessage':False,'debug':False,'heartInterval':30000,'reconnectInterval':5000}]}},
        'napcat.json':{'fileLog':False,'consoleLog':False,'fileLogLevel':'error','consoleLogLevel':'error'}}
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,data in files.items():archive.writestr(name,json.dumps(data,ensure_ascii=False,indent=2))
    return buffer.getvalue()
