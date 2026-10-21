"""Local TLS integration worker: launched with -S, never uses external hosts."""
import sys
from pathlib import Path
# -S excludes startup .pth hooks, including pip-system-certs.
root=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(root),sys.argv[1]]
import json,ssl,threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from types import SimpleNamespace
import boto3
from botocore.config import Config
from src.network_config import NetworkSettings
from src.data.network import secure_session,install_s3_tls

certificate,key,bundle=sys.argv[2:5]
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
        self.send_response(200);self.send_header('Content-Length','2');self.end_headers();self.wfile.write(b'ok')
    def do_HEAD(self):
        self.send_response(200);self.send_header('Content-Length','2');self.end_headers()
server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(certificate,key)
server.socket=context.wrap_socket(server.socket,server_side=True)
worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
endpoint='https://localhost:'+str(server.server_port)
settings=NetworkSettings(tls_mode='bundle',ca_bundle=Path(bundle))
results={}
try:
    session=secure_session(settings)
    try:
        response=session.get(endpoint,verify=True,timeout=3)
        results['requests']={'accepted':True,'status':response.status_code}
    except Exception as error:results['requests']={'accepted':False,'error':type(error).__name__}
    finally:session.close()
    # The local endpoint is only an internal transport test, with synthetic keys.
    # Production factories strictly validate the official CDSE endpoint allowlist.
    local=SimpleNamespace(**{**settings.__dict__,'s3_endpoint':endpoint})
    client=boto3.client('s3',endpoint_url=endpoint,aws_access_key_id='unit-access',
                        aws_secret_access_key='unit-secret',region_name='default',verify=True,
                        config=Config(retries={'total_max_attempts':1},connect_timeout=3,
                                      read_timeout=3,s3={'addressing_style':'path'}))
    try:
        install_s3_tls(client,local)
        result=client.head_object(Bucket='eodata',Key='test')
        results['botocore']={'accepted':True,'status':result['ResponseMetadata']['HTTPStatusCode']}
    except Exception as error:results['botocore']={'accepted':False,'error':type(error).__name__}
    finally:client.close()
    results['ssl_global_unchanged']=ssl.SSLContext.__module__=='ssl'
    print(json.dumps(results))
finally:
    server.shutdown();server.server_close();worker.join(timeout=5)
