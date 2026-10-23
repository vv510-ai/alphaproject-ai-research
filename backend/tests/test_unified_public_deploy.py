"""Public handoff checks: a healthy Lab alone cannot certify site ownership."""
import httpx
import pytest
from deployment.cloud.deploy import URL, verify_public_owner


@pytest.mark.parametrize('fault', [None, 'absolute_redirect', 'root_old', 'redirect_wrong', 'read_live', 'write_live', 'unknown_migration'])
def test_unified_owner_requires_root_and_retired_reads_and_writes(fault):
    release = {'release_id': 'lab-new', 'app_version': '0.11.1'}
    seen = []
    def handle(request):
        seen.append((request.method, request.url.path))
        if request.url.path == '/lab/api/legacy/migration':
            return httpx.Response(200, json={'status': 'unknown' if fault == 'unknown_migration' else 'imported'})
        if request.url.path == '/release.json':
            return httpx.Response(200, json={'release_id': 'old'} if fault == 'root_old' else release)
        if request.url.path == '/':
            return httpx.Response(308, headers={'location': '/old/' if fault == 'redirect_wrong' else URL if fault == 'absolute_redirect' else '/lab/'})
        assert request.url.path == '/watchlist'
        assert request.headers['Origin'] == 'https://8.155.130.86'
        live = fault == ('read_live' if request.method == 'GET' else 'write_live')
        return httpx.Response(200 if live else 410, json={'error': 'original_api_retired'})
    with httpx.Client(base_url=URL, transport=httpx.MockTransport(handle)) as client:
        if fault not in (None, 'absolute_redirect'):
            with pytest.raises(RuntimeError):
                verify_public_owner(client, release)
        else:
            assert verify_public_owner(client, release)['site_mode'] == 'unified'
            assert ('GET', '/watchlist') in seen and ('POST', '/watchlist') in seen


@pytest.mark.parametrize('api_status', [200, 404])
def test_pre_handoff_retains_original_release_guard(api_status):
    def handle(request):
        if request.url.path == '/lab/api/legacy/migration':
            return httpx.Response(api_status, json={'status': 'not_imported'})
        return httpx.Response(200, json={'release_id': 'alphaproject-20261015T211612763703Z'})
    with httpx.Client(base_url=URL, transport=httpx.MockTransport(handle)) as client:
        assert verify_public_owner(client, {'release_id': 'lab-new'}) == {
            'site_mode': 'separate', 'original_release_unchanged': True}
