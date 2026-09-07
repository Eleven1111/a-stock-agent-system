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


def test_per_dependency_partial_acceptance_is_narrow(monkeypatch):
    artifacts = {
        'capital-flow': {
            'run_id': 'capital', 'batch_id': 'test', 'trading_date': '2026-09-04',
            'artifact_path': '/tmp/capital.json', 'status': 'partial',
            'finished_at': '2026-09-04T09:00:00+08:00',
        },
        'hot-money-context': {
            'run_id': 'hot', 'batch_id': 'test', 'trading_date': '2026-09-04',
            'artifact_path': '/tmp/hot.json', 'status': 'partial',
            'finished_at': '2026-09-04T09:00:00+08:00',
        },
    }
    monkeypatch.setattr(
        runtime_context,
        'load_latest_artifact',
        lambda job_id, **_kwargs: artifacts[job_id],
    )
    policy = {
        'trading_date': 'same_trading_date',
        'accepted_statuses_by_job': {'capital-flow': ['ok', 'partial']},
    }

    gate = runtime_context.evaluate_dependencies(
        ['capital-flow', 'hot-money-context'],
        trading_date='2026-09-04',
        batch_id='test',
        policy=policy,
        now='2026-09-04T09:01:00+08:00',
    )

    assert gate['passed'] is False
    assert gate['dependencies'][0]['gate_status'] == 'passed'
    assert gate['dependencies'][0]['accepted_statuses'] == ['ok', 'partial']
    assert gate['dependencies'][1]['gate_status'] == 'blocked'
    assert gate['dependencies'][1]['accepted_statuses'] == ['ok']

    artifacts['hot-money-context']['status'] = 'ok'
    accepted_gate = runtime_context.evaluate_dependencies(
        ['capital-flow', 'hot-money-context'],
        trading_date='2026-09-04',
        batch_id='test',
        policy=policy,
        now='2026-09-04T09:01:00+08:00',
    )
    assert accepted_gate['passed'] is True


def test_per_dependency_override_never_accepts_degraded(monkeypatch):
    artifact = {
        'run_id': 'capital', 'batch_id': 'test', 'trading_date': '2026-09-04',
        'artifact_path': '/tmp/capital.json', 'status': 'degraded',
        'finished_at': '2026-09-04T09:00:00+08:00',
    }
    monkeypatch.setattr(runtime_context, 'load_latest_artifact', lambda *a, **kw: artifact)

    gate = runtime_context.evaluate_dependencies(
        ['capital-flow'],
        trading_date='2026-09-04',
        batch_id='test',
        policy={
            'trading_date': 'same_trading_date',
            'accepted_statuses_by_job': {'capital-flow': ['ok', 'partial']},
        },
        now='2026-09-04T09:01:00+08:00',
    )

    assert gate['passed'] is False
    assert gate['dependencies'][0]['reasons'] == ['status_degraded']
