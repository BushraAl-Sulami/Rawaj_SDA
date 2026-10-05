"""Launch both separate interfaces against the same Rawaj database and backend.

Agency: /agency on the API port. Restaurant: the unchanged Streamlit application.
No agency login is added. Bind locally for the internal demonstration.
"""
from pathlib import Path
import argparse
import os
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8010)
    parser.add_argument('--client-port',type=int,default=8502)
    parser.add_argument('--pause-automation',action='store_true',help='Pause scheduled follow-ups and Strategy while reviewing the UI.')
    args=parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env',override=False)
    # Respect an explicitly configured database; otherwise use the original shared rawaj.db.
    os.environ.setdefault('DATABASE_URL','sqlite:///'+(ROOT/'rawaj.db').as_posix())
    os.environ.update(PUBLIC_BASE_URL=f'http://127.0.0.1:{args.port}',
        DASHBOARD_URL=f'http://127.0.0.1:{args.client_port}',
        RAWAJ_API_URL=f'http://127.0.0.1:{args.port}',
        RAWAJ_DEMO_LOGIN='false', ALLOW_LOCAL_BUTTON_DEMO='true')
    if args.pause_automation:
        os.environ['PIPELINE_AUTORUN']='false'
    else:
        os.environ['PIPELINE_AUTORUN']='true'
    os.environ['OUTREACH_AUTOSTART']='true'
    # Validate account provisioning before any live Strategy notification runs.
    # Existing account passwords depend on this stable secret; never rotate it implicitly.
    if len(os.getenv('ACCOUNT_SECRET', '')) < 32:
        raise RuntimeError('Set a stable ACCOUNT_SECRET of at least 32 characters in .env before starting. Existing accounts require their original secret.')
    import socket
    for port in (args.port,args.client_port):
        with socket.socket() as s:
            if s.connect_ex(('127.0.0.1',port))==0:
                raise RuntimeError(f'Port {port} is already in use. Choose other ports.')
    # The child inherits the same API URL and database configuration.
    client=subprocess.Popen([sys.executable,'-m','streamlit','run','rawaj_front/app.py',
        '--server.address','127.0.0.1','--server.port',str(args.client_port),
        '--server.headless','true','--browser.gatherUsageStats','false'],cwd=ROOT,env=os.environ.copy())
    try:
        import uvicorn
        from api.main import create_app
        from api.services import default_background_stages
        print(f'Agency workspace: http://127.0.0.1:{args.port}/agency')
        print(f'Restaurant interface: http://127.0.0.1:{args.client_port}')
        uvicorn.run(create_app(background_stages=default_background_stages()),host='127.0.0.1',port=args.port,access_log=False)
    finally:
        client.terminate()
        try:
            client.wait(timeout=10)
        except subprocess.TimeoutExpired:
            client.kill()


if __name__=='__main__':
    main()
