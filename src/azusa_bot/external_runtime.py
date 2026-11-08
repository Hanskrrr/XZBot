"""Externally managed native NapCat; XZBot controls only its own bridge."""
import io
import json
import platform
import zipfile
from .settings import token
from .runtime_layout import runtime_layout


def enabled(root):
    marker=root/'config/runtime.json'
    if not marker.exists():return platform.system()!='Darwin'
    if marker.is_symlink() or marker.stat().st_size>1024:raise ValueError('runtime_config_invalid')
    data=json.loads(marker.read_text())
    if data not in ({'mode':'external'}, {'mode':'lima'}):raise ValueError('runtime_config_invalid')
    return data['mode']=='external'


def connection_archive(root):
    layout=runtime_layout(root)
    files={
        'webui.json':{'host':'127.0.0.1','port':layout.napcat_port,'token':token(root,'napcat-webui'),
                      'autoLoginAccount':'','disableWebUI':False},
        'onebot11.json':{'network':{'websocketClients':[{'name':'xzbot-local','enable':True,
            'url':f'ws://127.0.0.1:{layout.onebot_port}/ws','token':token(root,'onebot'),
            'messagePostFormat':'array','reportSelfMessage':False,'debug':False,'heartInterval':30000,'reconnectInterval':5000}]}},
        'napcat.json':{'fileLog':False,'consoleLog':False,'fileLogLevel':'error','consoleLogLevel':'error'}}
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,data in files.items():archive.writestr(name,json.dumps(data,ensure_ascii=False,indent=2))
    return buffer.getvalue()
