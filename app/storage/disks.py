"""Disk identity is verified before any destination write. Source is read-only."""
import ctypes
import sys
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid
from ..config import DATA
from ..telemetry import logger, utcnow, write_json

class DiskError(OSError):
    pass

# Prefixes of the stand-ins used when Windows reports no hardware serial. A hardware serial never has them.
FALLBACK_SERIALS = ('partition:', 'mbr:')

# Some disks report an empty SerialNumber: Hyper-V virtual disks and some USB enclosures. Their partition
# is named instead, like the Mac's partition UUID: the GPT partition GUID, or for MBR the disk signature
# and the partition's offset. A disk that reports a serial keeps exactly that value.
DISK_SERIAL = r'''
      $serial=([string]$disk.SerialNumber).Trim()
      if (-not $serial) {
        $partition=([string]$p.Guid).Trim().Trim('{}').ToLower()
        if ($partition) { $serial='partition:'+$partition }
        elseif ($disk.Signature) { $serial='mbr:'+('{0:x8}' -f [uint32]$disk.Signature)+':'+[string]$p.Offset }
      }
'''

def match_disk(expected, observed):
    for key in ('serial','volume_serial','volume_id'):
        recorded = str(expected[key]).strip().lower()
        seen = str(observed.get(key,'')).strip().lower()
        if key == 'serial' and not recorded and seen.startswith(FALLBACK_SERIALS):
            # Recorded before the fallback existed, when this disk's serial read as empty.
            continue
        if recorded != seen:
            raise DiskError(f'{expected["drive"]}: disk identity mismatch ({key}). No writes permitted.')

def snapshot(drives,since=None):
    if sys.platform == 'darwin':
        from .macos_disks import snapshot as mac_snapshot
        return mac_snapshot(drives)
    letters = ','.join(f"'{d}'" for d in drives)
    script = r'''
    $ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new()
    $result=@(foreach ($letter in @(LETTERS)) {
      $p=Get-Partition -DriveLetter $letter; $disk=$p | Get-Disk
      $logical=Get-CimInstance Win32_LogicalDisk -Filter ("DeviceID='"+$letter+":'")
      $guid=($p.AccessPaths | Where-Object {$_ -like '*Volume{*'}) -replace '^.*\{','' -replace '\}.*$',''
      DISKSERIAL
      [PSCustomObject]@{drive=$letter;serial=$serial;volume_serial=$logical.VolumeSerialNumber;volume_id=[string]$guid;online=(($disk.OperationalStatus -join ',') -eq 'Online');disk_number=$disk.Number}
    })
    EVENTCHECK
    ConvertTo-Json -InputObject $result -Compress
    '''.replace('LETTERS',letters).replace('DISKSERIAL',DISK_SERIAL)
    events=''
    if since is not None:
        since=datetime.fromisoformat(since).isoformat()
        events=r'''
        $events=@(); try {$events=@(Get-WinEvent -FilterHashtable @{LogName='System';Id=7,11,51,129,153,157;StartTime=[datetime]'SINCE'} -MaxEvents 50)}
        catch {if($_.FullyQualifiedErrorId -notlike 'NoMatchingEventsFound*'){throw}}
        foreach ($event in $events) {
          if ($event.ProviderName -eq 'UASPStor' -and $event.Id -eq 129) {throw 'New USB storage controller reset'}
          foreach ($disk in $result) {
            if ($event.ProviderName -eq 'disk' -and ($event.Message -match ('Harddisk'+$disk.disk_number+'\\') -or $event.Message -match ('(?:disk|磁盘) '+$disk.disk_number+'(?:\D|$)'))) {
              throw ('New storage error: event '+$event.Id+' disk '+$disk.disk_number)
            }
          }
        }
        '''.replace('SINCE',since)
    script=script.replace('EVENTCHECK',events)
    if any(len(d)!=1 or not d.isalpha() for d in drives):
        raise ValueError('Invalid drive letter')
    result = subprocess.run(['powershell.exe','-NoProfile','-Command',script], capture_output=True, timeout=15,
                            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode:
        if b'CmdletizationQuery_NotFound_DriveLetter' in result.stderr:
            raise DiskError('Selected disk is disconnected or unavailable. Reconnect the selected disk to access it.')
        raise DiskError('Windows disk identity query failed: '+result.stderr.decode(errors='replace')[:400])
    return {d['drive']:d for d in json.loads(result.stdout.decode('utf-8-sig'))}

def volume_serial(drive):
    if sys.platform == 'darwin':
        from .macos_disks import snapshot as mac_snapshot
        return mac_snapshot([drive])[drive]['volume_serial']
    if os.name != 'nt':
        raise DiskError('Automatic removable disk checks currently require Windows.')
    serial = ctypes.c_ulong()
    if not ctypes.windll.kernel32.GetVolumeInformationW(f'{drive}:\\',None,0,ctypes.byref(serial),None,None,None,0):
        raise DiskError(f'{drive}: volume inaccessible (Windows error {ctypes.windll.kernel32.GetLastError()})')
    return f'{serial.value:08X}'

class Monitor:
    def __init__(self, config, roles=('source','destination')):
        from ..config import require_destination
        require_destination(config)
        self.config=config
        if roles not in (('source','destination'),('destination',)):
            raise ValueError('Destination disk monitoring is mandatory')
        self.roles=roles
        self.log=logger('disk-monitor')
        self.stopping=threading.Event()
        self.last_ok=0
        self.error=None
        self.marker=Path(config['destination']['root']).parent / '.familyfoto-volume.json'
        self.marker_id=None
        self.since=datetime.now(timezone.utc).isoformat()

    def verify(self):
        disks=snapshot(list(dict.fromkeys(self.config[role]['drive'] for role in self.roles if self.config[role].get('kind')!='network')),self.since)
        for role in self.roles:
            expected=self.config[role]
            if expected.get('kind')=='network':
                from ..imports.manual import source_folder
                source_folder(self.config,expected['root'])
                disks['network-source']={'root':expected['root'],'online':True,'identity':'network path; file checksums verified'}
                continue
            observed=disks.get(expected['drive'],{})
            match_disk(expected,observed)
            if sys.platform == 'darwin':
                from .macos_disks import verify_path
                verify_path(expected, writable=role == 'destination')
            if not observed['online']:
                raise DiskError(f'{role} disk offline')
        return disks

    def prepare(self):
        self.verify()
        if self.marker.is_symlink():
            raise DiskError('Linked library marker is not allowed')
        if self.marker.exists():
            marker=json.loads(self.marker.read_text(encoding='utf-8'))
            match_disk(self.config['destination'],marker)
            self.marker_id=marker['library_id']
        else:
            self.marker.parent.mkdir(parents=True,exist_ok=True)
            self.marker_id=str(uuid.uuid4())
            write_json(self.marker,{**self.config['destination'],'library_id':self.marker_id})
        Path(self.config['destination']['root']).mkdir(exist_ok=True)
        self.last_ok=time.monotonic()
        self.thread=threading.Thread(target=self.run,daemon=True)
        self.thread.start()

    def run(self):
        while not self.stopping.is_set():
            try:
                begun=time.monotonic()
                self.log.info('identity-check-start')
                disks=self.verify()
                self.last_ok=time.monotonic()
                write_json(DATA/'monitor.json',{'state':'healthy','updated':utcnow(),'disks':disks,'check_seconds':round(self.last_ok-begun,3)})
                self.log.info('identity-check-ok duration=%.3fs',self.last_ok-begun)
            except Exception as error:
                self.error=f'{type(error).__name__}: {error}'
                self.log.exception('disk-monitor-fault')
                write_json(DATA/'monitor.json',{'state':'error','updated':utcnow(),'reason':self.error})
                return
            self.stopping.wait(5)

    def check(self):
        if self.error:
            raise DiskError(self.error)
        age=time.monotonic()-self.last_ok
        if age>60:
            raise DiskError(f'Disk monitor heartbeat expired: {age:.1f}s')
        for role in self.roles:
            expected=self.config[role]
            if expected.get('kind')=='network':
                from ..imports.manual import source_folder
                source_folder(self.config,expected['root'])
                continue
            if sys.platform == 'darwin':
                from .macos_disks import fast_verify_path
                fast_verify_path(expected, writable=role == 'destination')
            elif volume_serial(expected['drive'])!=expected['volume_serial'].upper():
                raise DiskError(f'{role} volume changed or disconnected')
        marker=json.loads(self.marker.read_text(encoding='utf-8'))
        if marker.get('library_id') != self.marker_id:
            raise DiskError('Destination library marker changed')

    def close(self):
        self.stopping.set()
        self.thread.join(timeout=17)
