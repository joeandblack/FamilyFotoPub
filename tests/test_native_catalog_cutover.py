import json
import pytest
from face_detect.api.readonly_people import catalog_settings
from face_detect.recognition.policy import inactive_model, legacy_model, matching_policy


def test_preserved_native_catalog_identity(tmp_path):
    pilot='a'*32
    (tmp_path/'people-preview.json').write_text(json.dumps({'enabled':True,'backend':'native','pilot':pilot}))
    root=tmp_path/'face_detect/profiles'/pilot
    root.mkdir(parents=True)
    runtime={'schema':'people','preserved_catalog':True,'legacy_pilot':'b'*32}
    (root/'runtime.json').write_text(json.dumps(runtime))
    assert catalog_settings(tmp_path)['legacy_pilot']=='b'*32
    assert catalog_settings(tmp_path)['native'] is True
    assert catalog_settings(tmp_path)['schema']=='people'
    assert catalog_settings(tmp_path)['thumbnails']==(tmp_path/'people/thumbnails').resolve()
    # A library from before the one catalog is migrated when it's activated.
    runtime['schema']='face_native_'+pilot
    (root/'runtime.json').write_text(json.dumps(runtime))
    with pytest.raises(ValueError,match='run the catalog migration'):catalog_settings(tmp_path)
    runtime['schema']='public'
    (root/'runtime.json').write_text(json.dumps(runtime))
    with pytest.raises(ValueError):catalog_settings(tmp_path)


class Catalog:
    def __init__(self, active):self.active=active
    def execute(self, query, params=()):
        if 'catalog_state' in query:self.row={'active_space_id':self.active} if self.active else None
        elif "state='active'" in query:self.row=None
        else:raise AssertionError('Historical model must not be used for matching')
        return self
    def fetchone(self):return self.row


def test_historical_embeddings_never_drive_native_matching():
    db=Catalog('native-model')
    assert inactive_model(db,'old-model')
    assert not legacy_model(db,'old-model')
    assert matching_policy(db,'old-model')['calibrated'] is False
    assert not inactive_model(db,'native-model')


def test_unmigrated_catalog_not_changed():
    assert not inactive_model(Catalog(None),'old-model')
