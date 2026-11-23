"""Keep retired container artifacts out of the native runtime."""
from pathlib import Path

import pytest

from face_detect.jobs.automatic_people_handoff import accept_checkpoint
from face_detect.testing.provider import FixtureProvider


ROOT = Path(__file__).resolve().parents[1]


def test_no_container_deployment_or_adapter_files():
    assert not list((ROOT/'config/compreface').rglob('*.*'))
    assert not list((ROOT/'face_detect/legacy').glob('*.py'))


def test_obsolete_checkpoint_rejected_before_database_access(monkeypatch):
    monkeypatch.setattr('face_detect.recognition.automatic_people.guard', lambda: None)
    with pytest.raises(ValueError, match='completed versioned jobs'):
        accept_checkpoint(None, {'schema': 'compreface-eval-job-v1', 'state': 'completed'},
                          {'username': 'test', 'role': 'admin'})


def test_synthetic_provider_verifies_evidence_and_uses_shared_service_contract():
    from PIL import Image
    from face_detect.contracts import ProviderUnavailable

    provider = FixtureProvider()
    record = provider.observe()
    provider.verify(record)
    provider.validate(record['stamp'])
    result = provider.embed_result(Image.new('RGB', (100, 100)), [0, 0, 100, 100])
    assert result['model_id'] == provider.identity
    assert result['source_size'] == [100, 100]
    assert result['seed']
    record['stamp']['model_id'] = 'changed'
    with pytest.raises(ProviderUnavailable):
        provider.verify(record)
