from test_automatic_people import automatic,pool,env,observations
from app import database
from app.config import ROOT
from face_detect.recognition import automatic_people as auto,people_auto_organize as organize
from psycopg.types.json import Jsonb


def test_calibrated_policy_groups_existing_faces_and_undo_preserves_labels(automatic):
    with database.connection() as db:
        db.execute("UPDATE space SET manifest=%s WHERE space_id='synthetic-auto'",(Jsonb({'dimensions':2,'schema':'familyfoto-native-model-v1','matching':{'mode':'review-only'}}),))
        auto.ingest(db,'native-test','synthetic-auto',observations(automatic))
        assert len(auto.catalog(db)['groups'])==4
        auto.initialize_review_sections(db,[r['media_id'] for r in auto.rows(db)])
        db.execute((ROOT/'face_detect/storage/matching_policy.sql').read_text())
        policy={'calibrated':True,'threshold':.55,'margin':.2,'minimum_detection_score':.8}
        db.execute("INSERT INTO policy(policy_id,space_id,policy,evidence) VALUES('fixture-policy','synthetic-auto',%s,'{}')",(Jsonb(policy),))
        data=organize.snapshot(db);proposal=organize.plan(data)
        assert proposal['summary']['regrouped_faces']==2
        result=organize.apply(db,data,proposal,'test')
        assert len(auto.catalog(db)['groups'])==2
        undone=organize.undo(db,result['event_id'],'test')
        assert undone['restored']==2 and len(auto.catalog(db)['groups'])==4
