"""Business outcomes preserve dependency safety without blocking ready data."""

import json

import pytest

import runtime_context


@pytest.mark.parametrize(
    'payload_status, expected',
    [('ready', 'ok'), ('no_signal', 'ok'), ('partial', 'partial'),
     ('insufficient_data', 'insufficient_data'), ('degraded', 'degraded'),
     ('unavailable', 'unavailable'), ('failed', 'failed'), ('blocked', 'blocked')],
)
def test_payload_status_preserves_dependency_contract(monkeypatch, payload_status, expected):
    artifact = runtime_context.build_artifact(
        job={'id': 'producer'}, run_id='test', command='x', cwd='.',
        returncode=0, stdout=json.dumps({'status': payload_status}), stderr='',
        started_at='2026-09-04T09:00:00+08:00',
        finished_at='2026-09-04T09:00:01+08:00', duration_seconds=1,
        context_artifacts=[],
    )
    assert artifact['status'] == expected
    monkeypatch.setattr(runtime_context, 'load_latest_artifact', lambda *a, **kw: artifact)
    result = runtime_context.evaluate_dependencies(
        ['producer'], trading_date='2026-09-04', batch_id='test',
        policy={'trading_date': 'latest'}, now='2026-09-04T09:01:00+08:00',
    )
    assert result['passed'] is (expected == 'ok')
