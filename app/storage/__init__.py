"""On-demand, read-only capacity snapshots of the enrolled volumes."""
import shutil
import sys
import subprocess
from .disks import snapshot, match_disk, volume_serial
from ..telemetry import utcnow


def capacity(config, source_root=None):
    result={'checked_at':utcnow()}
    for role in ('source','destination'):
        expected=config[role]
        row={key:expected.get(key) for key in ('root','serial','volume_serial')}
        if role=='source' and source_root:
            row['root']=source_root
        if not row['root']:
            row['error']='Not selected yet; choose storage when importing.'
            result[role]=row
            continue
        try:
            if expected.get('kind')=='unverified':
                raise ValueError('Source disk identity needs review; the latest import path is shown without checking another disk.')
            if expected.get('kind')=='network':
                from ..imports.manual import source_folder
                source_folder(config,expected['root'])
                row.update(zip(('total','used','free'),shutil.disk_usage(expected['root'])))
                row['identity']='Network path; hardware identity is unavailable'
                result[role]=row
                continue
            observed=snapshot([expected['drive']])[expected['drive']]
            match_disk(expected,observed)
            if not observed['online']:
                raise OSError('Disk offline')
            if sys.platform == 'darwin':
                from .macos_disks import verify_path
                verify_path(expected, writable=role == 'destination')
            usage=shutil.disk_usage(expected['drive'] if sys.platform == 'darwin' else expected['drive']+':\\')
            if volume_serial(expected['drive']).upper()!=expected['volume_serial'].upper():
                raise OSError('Disk changed during capacity check')
            row.update(total=usage.total,used=usage.used,free=usage.free)
        except (OSError,ValueError,KeyError,subprocess.TimeoutExpired) as error:
            row['error']=str(error)
        result[role]=row
    return result
