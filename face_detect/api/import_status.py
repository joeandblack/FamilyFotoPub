"""Per-import People progress, limited to imports the current viewer may see."""
import re


def import_statuses(catalog, jobs, root, user):
    result = {selection['id'].split(':')[1]: {'state': 'not-started', 'done': 0, 'total': selection['total'], 'excluded': 0, 'errors': []}
              for selection in catalog.selections(cache_seconds=60)}
    visible = catalog.visible_job_ids()
    for path in (root / 'jobs').glob('import-*/import.json'):
        name = path.parent.name
        if re.fullmatch(r'import-[1-9][0-9]*', name) and int(name.removeprefix('import-')) in visible:
            result[name.removeprefix('import-')] = jobs.status(name, user)
    return result
