from face_detect.recognition import automatic_people as auto


def test_ignored_pages_hold_100_faces(monkeypatch):
    monkeypatch.setattr(auto, 'guard', lambda: None)
    monkeypatch.setattr(auto.people, 'revision', lambda db: 7)

    class Database:
        def execute(self, sql, params=()):
            if 'count(*)' in sql:
                self.result = {'n': 201}
            else:
                assert 'LIMIT 100 OFFSET %s' in sql
                assert params == (100,)
                self.result = [{'detection_id': i} for i in range(100, 200)]
            return self

        def fetchone(self):
            return self.result

        def fetchall(self):
            return self.result

    result = auto.ignored_collection(Database(), page=2)
    assert len(result['items']) == 100
    assert result['total'] == 201
    assert result['pages'] == 3
    assert result['page'] == 2
