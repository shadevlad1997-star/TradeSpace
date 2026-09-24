import hashlib
import os
import uuid

import httpx
import pytest


_TEST_RUN_NAMESPACE = uuid.uuid4().hex


def _isolated_client_ip(node_id: str) -> str:
    worker_id = os.getenv('PYTEST_XDIST_WORKER', 'main')
    digest = hashlib.sha256(
        f'{_TEST_RUN_NAMESPACE}:{worker_id}:{node_id}'.encode()
    ).hexdigest()
    groups = [digest[index:index + 4] for index in range(0, 24, 4)]
    return '2001:db8:' + ':'.join(groups)


@pytest.fixture(autouse=True)
def isolated_rate_limit_ip(monkeypatch, request):
    if os.getenv('ENV', 'local').lower() != 'test':
        yield None
        return

    client_ip = _isolated_client_ip(request.node.nodeid)
    original_init = httpx.ASGITransport.__init__

    def isolated_transport_init(self, *args, **kwargs):
        client_was_positional = len(args) >= 4
        if not client_was_positional and 'client' not in kwargs:
            kwargs['client'] = (client_ip, 123)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.ASGITransport, '__init__', isolated_transport_init)
    yield client_ip
