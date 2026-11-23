"""The People service starts on a library whose database login can't create tables."""
from face_detect.jobs import automatic_people_local as module


class Db:
    def __init__(self, present):
        self.present, self.statements = present, []

    def execute(self, statement, *args):
        self.statements.append(statement)
        return self

    def fetchone(self):
        return {'present': self.present}


def test_an_existing_face_quality_table_is_not_created_again():
    """CREATE TABLE IF NOT EXISTS needs CREATE on the schema even when the table is there,
    and a recovered library's login has none."""
    db = Db(present=True)
    module.ensure_face_quality(db)
    assert len(db.statements) == 1 and 'CREATE' not in db.statements[0].upper()
    assert "to_regclass('auto_people_face_quality')" in db.statements[0]


def test_a_missing_face_quality_table_is_still_created():
    db = Db(present=False)
    module.ensure_face_quality(db)
    assert len(db.statements) == 2
    assert db.statements[1].lstrip().upper().startswith('CREATE TABLE IF NOT EXISTS AUTO_PEOPLE_FACE_QUALITY')


def test_the_catalog_itself_defines_the_table():
    """So on any library with the People catalog the table is always there."""
    from face_detect import catalog
    assert 'CREATE TABLE auto_people_face_quality(' in catalog.DDL.read_text()
