from __future__ import annotations
import os, socket, threading, webbrowser
from pathlib import Path
from feature_phone_clank.dashboard import serve


def main():
    state=Path(os.environ.setdefault('FEATURE_PHONE_CLANK_DATA_DIR',str(Path.home()/'Library'/'Application Support'/'Feature Phone Clank'))).expanduser().resolve(); state.mkdir(parents=True,exist_ok=True)
    with socket.socket() as s: s.bind(('127.0.0.1',0)); port=s.getsockname()[1]
    server=serve(port=port); threading.Timer(.3,webbrowser.open,args=(f'http://127.0.0.1:{port}/',)).start(); server.serve_forever()
if __name__=='__main__': main()
