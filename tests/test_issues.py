from app.jobs.issues import group_issues, group_import_issues

def test_groups_all_files_without_truncation_and_keeps_errors_separate():
    rows=[{'source_path':rf'F:\PHOTOS\{i}.XYZ','state':'unsupported','error':None} for i in range(250)]
    rows += [{'source_path':'F:/PHOTOS/last.xyz','state':'unsupported','error':None},
             {'source_path':'F:/PHOTOS/no-extension','state':'unsupported','error':None},
             {'source_path':'F:/PHOTOS/broken.JPG','state':'error','error':'Checksum mismatch'}]
    result=group_issues(rows)
    assert [(g['extension'],g['count']) for g in result['unsupported']]==[('(no extension)',1),('.xyz',251)]
    assert len(result['unsupported'][1]['files'])==251
    assert result['errors']==[rows[-1]]

def test_empty_review():
    assert group_issues([])=={'unsupported':[],'errors':[]}
    assert group_import_issues([])=={'imports':[]}


def test_imports_keep_same_extension_separate_and_sort_newest_first():
    rows=[{'job_id':job,'source_root':f'F:/PHOTOS/batch{job}',
           'started_at':'2026-10-10T12:00:00Z','job_state':'running',
           'source_path':f'F:/PHOTOS/batch{job}/file.XYZ','state':'unsupported','error':None}
          for job in (1,2,1)]
    rows.append({**rows[0],'state':'error','error':'Read failed'})
    batches=group_import_issues(rows)['imports']
    assert [b['job_id'] for b in batches]==[2,1]
    assert [b['unsupported_count'] for b in batches]==[1,2]
    assert len(batches[1]['errors'])==1
    assert not batches[0]['errors']
    assert all('batch2' in f['source_path'] for f in batches[0]['unsupported'][0]['files'])
