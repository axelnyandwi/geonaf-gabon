"""Real loopback TLS tests; no external network or persistent trust changes."""
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import trustme


@pytest.mark.parametrize('case',['valid','untrusted','wrong_hostname','expired'])
def test_verified_tls_for_requests_and_botocore_in_clean_process(tmp_path,case):
    ca=trustme.CA()
    name='wrong-host.invalid' if case=='wrong_hostname' else 'localhost'
    options={}
    if case=='expired':
        now=datetime.now(timezone.utc)
        options={'not_before':now-timedelta(days=3),'not_after':now-timedelta(days=1)}
    certificate=ca.issue_cert(name,**options)
    cert_path=tmp_path/'server.pem';key_path=tmp_path/'server.key';bundle_path=tmp_path/'ca.pem'
    cert_path.write_bytes(b''.join(v.bytes() for v in certificate.cert_chain_pems))
    key_path.write_bytes(certificate.private_key_pem.bytes())
    trusted=trustme.CA() if case=='untrusted' else ca
    bundle_path.write_bytes(trusted.cert_pem.bytes())
    # No fixture key is committed, logged, or installed in the system store.
    worker=Path(__file__).with_name('tls_probe_worker.py')
    packages=Path(trustme.__file__).resolve().parent.parent
    result=subprocess.run([sys.executable,'-B','-S',str(worker),str(packages),
                           str(cert_path),str(key_path),str(bundle_path)],
                          capture_output=True,text=True,timeout=25,
                          env={**os.environ,'NO_PROXY':'localhost,127.0.0.1','no_proxy':'localhost,127.0.0.1'})
    assert result.returncode==0,'Local TLS worker failed; private diagnostics withheld'
    report=json.loads(result.stdout)
    accepted=case=='valid'
    for transport in ('requests','botocore'):
        assert report[transport]['accepted'] is accepted, (case,transport,report[transport])
        if not accepted:assert report[transport]['error']=='SSLError'
    assert report['ssl_global_unchanged']


def test_bundle_rejects_private_keys_and_missing_files_in_clean_process(tmp_path):
    ca=trustme.CA()
    invalid=tmp_path/'private-input.pem'
    invalid.write_bytes(ca.private_key_pem.bytes())
    packages=Path(trustme.__file__).resolve().parent.parent
    code="""import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from src.data.network import tls_context
from src.network_config import NetworkSettings,NetworkConfigurationError
for name in sys.argv[2:]:
 try:tls_context(NetworkSettings(tls_mode='bundle',ca_bundle=Path(name)))
 except NetworkConfigurationError:continue
 raise AssertionError('Invalid CA input accepted')
print('REJECTED')
"""
    result=subprocess.run([sys.executable,'-B','-S','-c',code,str(packages),str(invalid),
                           str(tmp_path/'missing.pem')],capture_output=True,text=True,timeout=15)
    assert result.returncode==0 and result.stdout.strip()=='REJECTED'
