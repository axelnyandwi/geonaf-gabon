"""Manual read-only STAC/S3 smoke test; output contains no credentials/paths.

Use --clean-process to run with -S, excluding pip-system-certs startup hooks.
Only one STAC item and a 1 KiB S3 range are read; no satellite file is written.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--clean-process',action='store_true')
    parser.add_argument('--packages',help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.packages:
        sys.path.insert(0,args.packages)
    if args.clean_process:
        import requests
        packages=Path(requests.__file__).resolve().parent.parent
        result=subprocess.run([sys.executable,'-B','-S',str(Path(__file__).resolve()),
                               '--packages',str(packages)],check=False)
        return result.returncode
    import ssl
    import time
    import warnings
    from urllib.parse import urlsplit
    import requests
    from src.data.ingestion import search_sentinel2,create_cdse_s3_client,STACSearchTruncatedWarning
    from src.data.network import NetworkError,run_with_retry
    from src.network_config import NetworkSettings,NetworkConfigurationError
    settings=NetworkSettings.from_environment()
    initial_ssl_class=ssl.SSLContext
    report={'tls_mode':settings.tls_mode,'startup_ssl_context':initial_ssl_class.__module__,
            'stac_host':urlsplit(settings.stac_endpoint).hostname,
            's3_host':urlsplit(settings.s3_endpoint).hostname}
    # Before/after use the same interpreter and current network, not stale logs.
    try:
        start=time.perf_counter()
        with requests.get(settings.stac_endpoint,timeout=(10,15),stream=True,
                          allow_redirects=False) as response:
            report['baseline_requests']={'status':response.status_code,
                                         'seconds':round(time.perf_counter()-start,3)}
    except requests.exceptions.RequestException as error:
        report['baseline_requests']={'error':type(error).__name__,
                                     'certificate_verify_failed':'CERTIFICATE_VERIFY_FAILED' in str(error)}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore',STACSearchTruncatedWarning)
            start=time.perf_counter()
            items=search_sentinel2([9.35,.48,9.45,.58],'2025-01-25','2025-01-25',max_items=1)
            report['stac']={'items':len(items),'seconds':round(time.perf_counter()-start,3)}
        if not items:
            raise NetworkError('Aucun actif de test disponible.',service='stac')
        uri=urlsplit(items[0]['assets']['B04_10m']['href'])
        if uri.scheme!='s3' or uri.netloc!='eodata':
            raise NetworkError('Actif de test S3 non autorisé.',service='s3')
        client=create_cdse_s3_client()
        try:
            key=uri.path.lstrip('/')
            start=time.perf_counter()
            head=run_with_retry(lambda:client.head_object(Bucket='eodata',Key=key),settings,service='s3')
            report['s3_head']={'status':head['ResponseMetadata']['HTTPStatusCode'],
                               'seconds':round(time.perf_counter()-start,3)}
            def read_range():
                response=client.get_object(Bucket='eodata',Key=key,Range='bytes=0-1023')
                body=response['Body']
                try:
                    payload=body.read(1024)
                    return {'status':response['ResponseMetadata']['HTTPStatusCode'],
                            'bytes_read':len(payload)}
                finally:body.close()
            report['s3_get']=run_with_retry(read_range,settings,service='s3')
            report['sdk_total_max_attempts']=client.meta.config.retries['total_max_attempts']
            context=client._endpoint.http_session.context
            report['tls_verified']=(context.check_hostname and context.verify_mode==ssl.CERT_REQUIRED)
        finally:client.close()
    except NetworkConfigurationError:
        report['failure']={'category':'configuration'}
    except NetworkError as error:
        report['failure']={'category':error.category,'status':error.status_code,
                           's3_code':error.s3_code,'attempts':error.attempts}
    report['ssl_global_unchanged']=ssl.SSLContext is initial_ssl_class
    print(json.dumps(report,indent=2))
    return int('failure' in report)


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except Exception:
        # Never print raw exceptions containing private paths, URLs or SDK data.
        print(json.dumps({'failure':{'category':'configuration_or_protocol'}}))
        raise SystemExit(1) from None
