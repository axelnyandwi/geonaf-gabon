"""Secure network configuration, retry, and ingestion regression tests."""
import ssl
from unittest.mock import Mock

import pytest
import requests
from pystac_client.exceptions import APIError

from src.data import ingestion
from src.network_config import NetworkSettings


def test_stac_preserves_pystac_http_status(monkeypatch):
    response = requests.Response()
    response.status_code = 403
    response._content = b'private diagnostic must not be printed'
    monkeypatch.setattr(ingestion.Client, 'open', Mock(side_effect=APIError.from_response(response)))
    with pytest.raises(ingestion.STACSearchError) as caught:
        ingestion.search_sentinel2([9.3,.3,9.6,.6], '2025-01-25', '2025-01-25')
    assert caught.value.status_code == 403
    assert caught.value.category == 'http'
    assert '403' in str(caught.value)
    assert 'private diagnostic' not in str(caught.value)


def test_network_settings_default_to_system_without_ssl_mutation(monkeypatch):
    monkeypatch.delenv('GEONAF_TLS_MODE', raising=False)
    monkeypatch.delenv('GEONAF_CA_BUNDLE', raising=False)
    before = ssl.SSLContext
    settings = NetworkSettings.from_environment(load_file=False)
    assert settings.tls_mode == 'system'
    assert settings.s3_endpoint == 'https://eodata.dataspace.copernicus.eu'
    assert ssl.SSLContext is before

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import logging
import math
import os
from pathlib import Path
import subprocess
import sys

from botocore.exceptions import ClientError, SSLError as S3SSLError
from src.data import network
from src.network_config import NetworkConfigurationError


@pytest.fixture(autouse=True)
def network_environment(monkeypatch):
    for key in list(os.environ):
        if key.startswith('GEONAF_'):
            monkeypatch.delenv(key)


@pytest.mark.parametrize('status', [401,403,429,500,502,503,504])
def test_stac_error_status_and_no_raw_response(monkeypatch, status):
    response = requests.Response();response.status_code=status
    response._content=b'Authorization: sensitive-test-placeholder'
    error=APIError.from_response(response)
    monkeypatch.setattr(ingestion.Client,'open',Mock(side_effect=error))
    with pytest.raises(ingestion.STACSearchError) as caught:
        ingestion.search_sentinel2([9.3,.3,9.6,.6],'2025-01-25','2025-01-25')
    assert caught.value.status_code == status
    assert 'sensitive-test-placeholder' not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize('variable,value', [
    ('GEONAF_TLS_MODE','disabled'), ('GEONAF_NETWORK_MAX_ATTEMPTS','0'),
    ('GEONAF_NETWORK_MAX_ATTEMPTS','11'), ('GEONAF_NETWORK_MAX_ATTEMPTS','private-invalid-value'),
    ('GEONAF_NETWORK_BACKOFF_SECONDS','nan'), ('GEONAF_NETWORK_MAX_DELAY_SECONDS','inf'),
    ('GEONAF_NETWORK_CONNECT_TIMEOUT','0'), ('GEONAF_NETWORK_READ_TIMEOUT','121'),
    ('GEONAF_NETWORK_MAX_DELAY_SECONDS','301'), ('GEONAF_NETWORK_WAIT_BUDGET_SECONDS','601'),
])
def test_invalid_settings_are_rejected_without_echo(monkeypatch,variable,value):
    monkeypatch.setenv(variable,value)
    with pytest.raises(NetworkConfigurationError) as caught:
        NetworkSettings.from_environment(load_file=False)
    assert 'private-invalid-value' not in str(caught.value)


@pytest.mark.parametrize('endpoint', [
    'http://eodata.dataspace.copernicus.eu',
    'https://eodata.dataspace.copernicus.eu.attacker.invalid',
    'https://access:private-test-placeholder@eodata.dataspace.copernicus.eu',
    'https://eodata.dataspace.copernicus.eu/?token=private-test-placeholder',
    'https://eodata.dataspace.copernicus.eu:8443',
    'https://eodata.dataspace.copernicus.eu/private',
])
def test_endpoint_rejected_before_credentials(endpoint):
    with pytest.raises(NetworkConfigurationError) as caught:
        NetworkSettings(s3_endpoint=endpoint)
    assert 'private-test-placeholder' not in str(caught.value)


def test_configured_endpoints_and_budgets_are_shared(monkeypatch):
    monkeypatch.setenv('GEONAF_S3_ENDPOINT','https://eodata.ams.dataspace.copernicus.eu/')
    monkeypatch.setenv('GEONAF_STAC_ENDPOINT','https://stac.dataspace.copernicus.eu/v1/')
    monkeypatch.setenv('GEONAF_NETWORK_MAX_ATTEMPTS','4')
    settings=NetworkSettings.from_environment(load_file=False)
    assert settings.s3_endpoint=='https://eodata.ams.dataspace.copernicus.eu'
    assert settings.stac_endpoint.endswith('/v1')
    assert settings.max_attempts==4


@pytest.mark.parametrize('mode,bundle', [('bundle',None),('system',Path('unused-ca.pem'))])
def test_bundle_configuration_is_unambiguous(mode,bundle):
    with pytest.raises(NetworkConfigurationError):
        NetworkSettings(tls_mode=mode,ca_bundle=bundle)


def test_system_context_is_verified_and_does_not_inject():
    before=ssl.SSLContext
    context=network.tls_context(NetworkSettings())
    assert context.verify_mode==ssl.CERT_REQUIRED
    assert context.check_hostname
    assert context.minimum_version>=ssl.TLSVersion.TLSv1_2
    assert ssl.SSLContext is before


def test_bad_bundle_fails_without_path_disclosure(tmp_path, monkeypatch):
    # Validation also runs in a clean subprocess in the TLS integration tests.
    settings=NetworkSettings(tls_mode='bundle',ca_bundle=tmp_path/'private-name.pem')
    with pytest.raises(NetworkConfigurationError) as caught:
        network.tls_context(settings)
    assert str(tmp_path) not in str(caught.value)


def test_http_and_unverified_requests_are_rejected_before_send():
    session=network.secure_session(NetworkSettings())
    try:
        with pytest.raises(NetworkConfigurationError):session.get('http://localhost/',timeout=.1)
        adapter=session.get_adapter('https://')
        request=requests.Request('GET','https://stac.dataspace.copernicus.eu/v1').prepare()
        with pytest.raises(NetworkConfigurationError):adapter.build_connection_pool_key_attributes(request,False)
        with pytest.raises(NetworkConfigurationError):adapter.cert_verify(Mock(),'https://localhost/',False,None)
        assert adapter.max_retries.total==0
        assert session.trust_env is False
    finally:session.close()


def s3_error(status,code='ServiceUnavailable',retry_after=None):
    metadata={'HTTPStatusCode':status,'HTTPHeaders':{}}
    if retry_after is not None:metadata['HTTPHeaders']['retry-after']=retry_after
    return ClientError({'Error':{'Code':code,'Message':'private-test-placeholder'},
                        'ResponseMetadata':metadata},'GetObject')


@pytest.mark.parametrize('status', [429,500,502,503,504])
def test_transient_http_retries_once_without_nested_retry(monkeypatch,status):
    action=Mock(side_effect=[s3_error(status), 'ok'])
    sleep=Mock();monkeypatch.setattr(network.time,'sleep',sleep)
    monkeypatch.setattr(network.random,'uniform',lambda a,b:b)
    assert network.run_with_retry(action,NetworkSettings(),service='s3')=='ok'
    assert action.call_count==2
    sleep.assert_called_once_with(1.)


@pytest.mark.parametrize('error,category', [
    (s3_error(403,'AccessDenied'),'s3'),
    (s3_error(401),'s3'),
    (requests.exceptions.SSLError('private-test-placeholder'),'tls'),
    (S3SSLError(endpoint_url='https://private.invalid',error='private-test-placeholder'),'tls'),
    (requests.exceptions.ProxyError('private-test-placeholder'),'proxy'),
])
def test_terminal_failures_are_never_retried_or_leaked(monkeypatch,caplog,error,category):
    caplog.set_level(logging.DEBUG)
    action=Mock(side_effect=error);sleep=Mock();monkeypatch.setattr(network.time,'sleep',sleep)
    with pytest.raises(network.NetworkError) as caught:
        network.run_with_retry(action,NetworkSettings(),service='s3')
    assert caught.value.category==category
    assert caught.value.attempts==1
    assert action.call_count==1
    sleep.assert_not_called()
    assert 'private-test-placeholder' not in caplog.text+str(caught.value)
    assert 'private.invalid' not in caplog.text+str(caught.value)


def test_retries_exhaust_global_attempt_budget(monkeypatch):
    action=Mock(side_effect=s3_error(503));sleeps=[]
    monkeypatch.setattr(network.time,'sleep',sleeps.append)
    monkeypatch.setattr(network.random,'uniform',lambda a,b:b)
    with pytest.raises(network.NetworkError) as caught:
        network.run_with_retry(action,NetworkSettings(),service='s3')
    assert action.call_count==3
    assert sleeps==[1.,2.]
    assert caught.value.attempts==3
    assert caught.value.status_code==503


def test_retry_after_seconds_and_dates():
    now=datetime(2026,10,21,tzinfo=timezone.utc)
    assert network.retry_after_seconds('7')==7
    assert network.retry_after_seconds(format_datetime(now+timedelta(seconds=9)),now=now)==9
    assert network.retry_after_seconds(format_datetime(now-timedelta(seconds=9)),now=now)==0
    for invalid in ['bad','nan','inf',None]:assert network.retry_after_seconds(invalid) is None


def test_retry_after_is_respected_but_not_shortened(monkeypatch):
    sleep=Mock();monkeypatch.setattr(network.time,'sleep',sleep)
    monkeypatch.setattr(network.random,'uniform',lambda a,b:0)
    action=Mock(side_effect=[s3_error(503,retry_after='7'),'ok'])
    assert network.run_with_retry(action,NetworkSettings(),service='s3')=='ok'
    sleep.assert_called_once_with(7)
    for delay in ('31','9999999999999'):
        action=Mock(side_effect=s3_error(503,retry_after=delay));sleep.reset_mock()
        with pytest.raises(network.NetworkError):network.run_with_retry(action,NetworkSettings(),service='s3')
        assert action.call_count==1
        sleep.assert_not_called()


def test_total_sleep_budget_is_not_exceeded(monkeypatch):
    action=Mock(side_effect=s3_error(503,retry_after='2'));sleeps=[]
    monkeypatch.setattr(network.time,'sleep',sleeps.append)
    monkeypatch.setattr(network.random,'uniform',lambda a,b:0)
    with pytest.raises(network.NetworkError):
        network.run_with_retry(action,replace(NetworkSettings(),wait_budget_seconds=3),service='s3')
    assert action.call_count==2 and sleeps==[2]


def test_upstream_debug_logs_are_suppressed_only_on_initialization(caplog):
    caplog.set_level(logging.DEBUG)
    network.quiet_transport_logs()
    logging.getLogger('botocore.auth').debug('private-test-placeholder')
    logging.getLogger('pystac_client.stac_api_io').debug('private-test-placeholder')
    logging.getLogger('urllib3.connectionpool').debug('private-test-placeholder')
    assert 'private-test-placeholder' not in caplog.text


def test_stac_requests_retry_status_but_do_not_log_body(monkeypatch,caplog):
    io=network.SecureStacIO(NetworkSettings())
    def response(status):
        value=Mock();value.status_code=status;value.headers={'Retry-After':'0'}
        value.content=b'{"ok":true}'
        value.__enter__=Mock(return_value=value);value.__exit__=Mock(return_value=None)
        return value
    request=Mock(side_effect=[response(503),response(200)])
    io.session.request=request
    monkeypatch.setattr(network.time,'sleep',Mock())
    assert io.request(NetworkSettings().stac_endpoint+'/search',method='POST',parameters={'test':1})=='{"ok":true}'
    assert request.call_count==2
    assert all(call.kwargs['verify'] is True and call.kwargs['allow_redirects'] is False for call in request.call_args_list)
    io.session.close()


@pytest.mark.parametrize('url', ['http://stac.dataspace.copernicus.eu/v1',
                                 'https://other.invalid/search',
                                 'https://secret@stac.dataspace.copernicus.eu/v1'])
def test_stac_pagination_cannot_escape_origin(url):
    io=network.SecureStacIO(NetworkSettings())
    try:
        with pytest.raises(NetworkConfigurationError):io.request(url)
    finally:io.session.close()


def test_download_returns_structured_s3_error_and_retains_cache(tmp_path,monkeypatch):
    client=Mock();client.download_file.side_effect=s3_error(403,'AccessDenied')
    with pytest.raises(ingestion.S3DownloadError) as caught:
        ingestion.download_asset(client,{'href':'s3://eodata/test.jp2'},tmp_path)
    assert caught.value.status_code==403
    assert caught.value.s3_code=='AccessDenied'
    assert client.download_file.call_count==1
    (tmp_path/'test.jp2').write_bytes(b'cached-data')
    ingestion.download_asset(client,{'href':'s3://eodata/test.jp2'},tmp_path)
    assert client.download_file.call_count==1


@pytest.mark.parametrize('key', ['../x/..', 'x/C:secret.jp2', 'x/..\\secret.jp2','x/','x/a.jp2?secret=test'])
def test_download_rejects_unsafe_names(tmp_path,key):
    with pytest.raises(ValueError):ingestion.download_asset(Mock(),{'href':'s3://eodata/'+key},tmp_path)


def test_import_does_not_inject_tls_or_load_dotenv():
    code="""import ssl,os,json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
identity=ssl.SSLContext
before=dict(os.environ)
import src.data.ingestion
assert ssl.SSLContext is identity
assert dict(os.environ)==before
print('IMPORT_SIDE_EFFECTS_OK')
"""
    result=subprocess.run([sys.executable,'-B','-S','-c',code,str(Path(requests.__file__).resolve().parent.parent)],capture_output=True,text=True)
    assert result.returncode==0, 'Clean-process import contract failed'
    assert 'IMPORT_SIDE_EFFECTS_OK' in result.stdout


def test_s3_factory_uses_endpoint_and_one_sdk_attempt(monkeypatch):
    import boto3
    before=ssl.SSLContext
    settings=NetworkSettings(s3_endpoint='https://eodata.ams.dataspace.copernicus.eu')
    client=network.create_s3_client(boto3,settings,'unit-access','unit-secret')
    try:
        assert client.meta.endpoint_url==settings.s3_endpoint
        assert client.meta.config.retries['total_max_attempts']==1
        assert client.meta.config.signature_version=='s3v4'
        assert isinstance(client._endpoint.http_session,network._S3TLS)
        assert client._endpoint.http_session.context.check_hostname
        assert ssl.SSLContext is before
    finally:client.close()


def test_s3_tls_is_validated_before_client_creation(monkeypatch):
    module=Mock()
    def invalid(*args):raise NetworkConfigurationError('test-invalid-CA')
    monkeypatch.setattr(network,'tls_context',invalid)
    with pytest.raises(NetworkConfigurationError):
        network.create_s3_client(module,NetworkSettings(),'unit-access','unit-secret')
    module.client.assert_not_called()


def test_factory_rejects_missing_credentials_without_revealing_values(monkeypatch):
    monkeypatch.setattr(NetworkSettings,'from_environment',lambda:NetworkSettings())
    monkeypatch.delenv('CDSE_S3_ACCESS_KEY',raising=False)
    monkeypatch.delenv('CDSE_S3_SECRET_KEY',raising=False)
    with pytest.raises(NetworkConfigurationError):ingestion.create_cdse_s3_client()


def test_client_creation_exception_is_redacted():
    module=Mock();module.client.side_effect=ValueError('private-test-placeholder')
    with pytest.raises(NetworkConfigurationError) as caught:
        network.create_s3_client(module,NetworkSettings(),'unit-access','unit-secret')
    assert 'private-test-placeholder' not in str(caught.value)
    assert caught.value.__cause__ is None


def test_s3_transport_rejects_other_hosts_and_plain_http():
    transport=network._S3TLS(NetworkSettings(),verify=True)
    try:
        for url in ['http://eodata.dataspace.copernicus.eu/', 'https://other.invalid/',
                    'https://secret@eodata.dataspace.copernicus.eu/']:
            with pytest.raises(NetworkConfigurationError):transport.send(Mock(url=url))
        with pytest.raises(NetworkConfigurationError):transport._setup_ssl_cert(Mock(),'https://localhost/',False)
        assert transport._setup_proxy_ssl_context('https://proxy.invalid') is transport.context
    finally:transport.close()


def test_nested_sdk_retries_rejected_for_download_and_metadata(tmp_path):
    import boto3
    from botocore.config import Config
    client=boto3.client('s3',endpoint_url=NetworkSettings().s3_endpoint,
                        aws_access_key_id='unit-access',aws_secret_access_key='unit-secret',
                        config=Config(retries={'total_max_attempts':3}))
    try:
        asset={'href':'s3://eodata/test.xml'}
        with pytest.raises(NetworkConfigurationError):ingestion.download_asset(client,asset,tmp_path)
        with pytest.raises(NetworkConfigurationError):
            ingestion._download_small_stac_asset(client,asset,tmp_path/'test.xml',1024)
    finally:client.close()


def test_managed_download_has_no_hidden_transfer_retries(monkeypatch,tmp_path):
    import boto3
    client=network.create_s3_client(boto3,NetworkSettings(),'unit-access','unit-secret')
    download=Mock();monkeypatch.setattr(client,'download_file',download)
    try:
        ingestion.download_asset(client,{'href':'s3://eodata/test.jp2'},tmp_path)
        config=download.call_args.kwargs['Config']
        assert config.num_download_attempts==1
        assert config.preferred_transfer_client=='classic'
        assert config.use_threads is False
    finally:client.close()


def test_metadata_read_retry_closes_failed_stream_before_retry(monkeypatch,tmp_path):
    from botocore.exceptions import ReadTimeoutError
    first=Mock();first.read.side_effect=ReadTimeoutError(endpoint_url='https://private.invalid')
    second=Mock();second.read.return_value=b'xml'
    client=Mock();client.get_object.side_effect=[{'Body':first,'ContentLength':3},
                                               {'Body':second,'ContentLength':3}]
    monkeypatch.setattr(network.time,'sleep',Mock())
    result=ingestion._download_small_stac_asset(client,{'href':'s3://eodata/test.xml'},tmp_path/'test.xml',10)
    assert result.read_bytes()==b'xml'
    assert client.get_object.call_count==2
    first.close.assert_called_once();second.close.assert_called_once()


def test_unknown_s3_error_codes_cannot_enter_logs(caplog):
    error=s3_error(403,'private-test-placeholder')
    with pytest.raises(network.NetworkError) as caught:
        network.run_with_retry(Mock(side_effect=error),NetworkSettings(),service='s3')
    assert caught.value.s3_code is None
    assert 'private-test-placeholder' not in caplog.text+str(caught.value)


def test_connection_timeout_retries_and_preserves_category(monkeypatch):
    action=Mock(side_effect=requests.exceptions.Timeout('private-test-placeholder'))
    monkeypatch.setattr(network.time,'sleep',Mock())
    with pytest.raises(network.NetworkError) as caught:
        network.run_with_retry(action,NetworkSettings(),service='stac')
    assert caught.value.category=='timeout' and caught.value.attempts==3


def test_transfer_wrapper_exposes_safe_underlying_category():
    from boto3.exceptions import RetriesExceededError
    original=requests.exceptions.Timeout('private-test-placeholder')
    result=network.classify_error(RetriesExceededError(original),'s3')
    assert result.category=='timeout'
    assert 'private-test-placeholder' not in str(result)


def test_proxy_pools_receive_same_verified_context():
    session=network.secure_session(NetworkSettings())
    try:
        adapter=session.get_adapter('https://')
        proxy=adapter.proxy_manager_for('https://proxy.invalid:443')
        assert proxy.connection_pool_kw['ssl_context'] is adapter.context
        assert proxy.proxy_ssl_context is adapter.context
    finally:session.close()



@pytest.mark.parametrize('source',['.env','file:///private-input','http://stac.dataspace.copernicus.eu/v1'])
def test_stac_reader_never_opens_local_files(source):
    io=network.SecureStacIO(NetworkSettings())
    try:
        with pytest.raises(NetworkConfigurationError):io.read_text(source)
    finally:io.session.close()


def test_configured_default_https_port_is_canonicalized(monkeypatch):
    monkeypatch.setenv('GEONAF_S3_ENDPOINT','https://EODATA.DATASPACE.COPERNICUS.EU:443/')
    assert NetworkSettings.from_environment(load_file=False).s3_endpoint==NetworkSettings().s3_endpoint
