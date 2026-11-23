"""Page unnamed group IDs in SQL before loading their face membership."""
from face_detect.recognition import automatic_people as auto
from face_detect.recognition.people_review_scope import media_ids


def review_page(db,section='review',page=1,page_size=100,import_id=None):
    auto.guard()
    scope=media_ids(db,import_id)
    scope_sql='' if scope is None else ' AND a.media_id=ANY(%s)'
    # The latest explicit deferral overrides the original review baseline.
    sql='''WITH baseline AS (
      SELECT before_state->'legacy' legacy FROM auto_people_events
      WHERE action='review-sections-baseline' ORDER BY id LIMIT 1
    ), deferred AS (
      SELECT DISTINCT ON (j.key) j.key,j.value members FROM auto_people_events ev
      CROSS JOIN LATERAL jsonb_each(ev.before_state->'groups') j
      WHERE ev.action='review-set-aside' ORDER BY j.key,ev.id DESC
    ), released_groups AS (
      SELECT DISTINCT m.group_id FROM auto_people_face_quality q
      JOIN auto_people_members m ON m.detection_id=q.detection_id WHERE q.state='released'
    ), eligible AS (
      SELECT d.id,a.media_id,m.captured_at '''+auto.ELIGIBLE+scope_sql+'''
    ), grouped AS (
      SELECT gm.group_id,min(e.id) first_face,count(*) face_count,
        count(DISTINCT coalesce(e.captured_at::date::text,'None'))<2 optional,
        bool_and(coalesce(deferred.members,(SELECT legacy FROM baseline)->gm.group_id::text,'[]'::jsonb)
                 @> jsonb_build_array(e.id)) previously_reviewed,
        bool_or(deferred.key IS NOT NULL) explicitly_deferred,
        bool_or(released_groups.group_id IS NOT NULL) released
      FROM eligible e JOIN auto_people_members gm ON gm.detection_id=e.id
      JOIN auto_people_groups g ON g.id=gm.group_id
      LEFT JOIN people_assignments pa ON pa.detection_id=e.id
      LEFT JOIN deferred ON deferred.key=gm.group_id::text
      LEFT JOIN released_groups ON released_groups.group_id=gm.group_id
      WHERE coalesce(pa.person_id,g.person_id) IS NULL GROUP BY gm.group_id
    ), classified AS (
      SELECT *,CASE WHEN explicitly_deferred OR EXISTS(SELECT 1 FROM baseline)
        THEN CASE WHEN previously_reviewed THEN 'other' ELSE 'review' END
        WHEN optional AND NOT coalesce(released,false) THEN 'other' ELSE 'review' END section
      FROM grouped
    ) '''
    params=() if scope is None else (list(scope),)
    totals={r['section']:r for r in db.execute(sql+'SELECT section,count(*) total,coalesce(sum(face_count),0) faces FROM classified GROUP BY section',params).fetchall()}
    total=totals.get(section,{}).get('total',0)
    pages=max(1,(total+page_size-1)//page_size);page=min(page,pages)
    order=auto.saved_order(db)
    ranks={}
    for i,item in enumerate(order):
        if item.get('group_id') is not None:ranks.setdefault(str(item['group_id']),i)
    from psycopg.types.json import Jsonb
    selected=db.execute(sql+'''SELECT group_id FROM classified WHERE section=%s
      ORDER BY coalesce((%s::jsonb->>group_id::text)::int,%s),first_face,group_id LIMIT %s OFFSET %s''',
      (*params,section,Jsonb(ranks),len(order),page_size,(page-1)*page_size)).fetchall()
    ids=[r['group_id'] for r in selected]
    view=auto.catalog(db,import_id=import_id,group_ids=ids)
    groups={g['id']:g for g in view['groups'] if g['person_id'] is None}
    return {**view,'groups':[groups[gid] for gid in ids if gid in groups],
            'page':page,'pages':pages,'page_size':page_size,'total':total,
            'section':section,'totals':{key:int(value['total']) for key,value in totals.items()},
            'outlier_faces':int(totals.get('other',{}).get('faces',0))}
