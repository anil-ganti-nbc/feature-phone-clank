from __future__ import annotations
import os, socket, threading, webbrowser, sys, time, urllib.request
from pathlib import Path
from feature_phone_clank.dashboard import serve


def main():
    state=Path(os.environ.get('FEATURE_PHONE_FIELD_TEST_HOME') or (Path.home()/'Library'/'Application Support'/'Feature Phone Clank')).expanduser().resolve(); state.mkdir(parents=True,exist_ok=True)
    os.environ['FEATURE_PHONE_CLANK_DATA_DIR']=str(state)
    os.environ['FEATURE_PHONE_CLANK_DISCORD_WEBHOOK_URL']=''
    if hasattr(sys,'_MEIPASS'): os.environ['FEATURE_PHONE_CLANK_CONFIG_ROOT']=str(Path(sys._MEIPASS).resolve())
    with socket.socket() as s: s.bind(('127.0.0.1',0)); port=s.getsockname()[1]
    from feature_phone_clank.local_collection import LocalCollectionController
    from feature_phone_clank.paths import resolve_config_path, resolve_data_path
    controller=LocalCollectionController(resolve_data_path('data/feature_phone_clank.db'),state/'feature-phone-clank.lock',resolve_config_path('scope.yaml'),resolve_config_path('hmd_overrides.yaml'))
    server=serve(port=port,controller=controller); url=f'http://127.0.0.1:{port}/'
    def ready():
        for _ in range(200):
            try:
                if urllib.request.urlopen(url+'healthz',timeout=1).status==200: webbrowser.open(url); return
            except Exception: time.sleep(.15)
    threading.Thread(target=ready,daemon=True).start(); server.serve_forever()
if __name__=='__main__': main()
