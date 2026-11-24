"""Release-side shared manifest and small Windows EXE builder. Does not publish."""
import argparse
import base64
import json
import os
import subprocess
import zipfile
from pathlib import Path
from installer.common.acquire import file_digest, plain_path
from installer.common.manifest import Manifest, safe_relative, sha256, digest_string


def audit_windows_release(manifest, payload_root):
    """Fail before assembly if required payloads/locks or local bytes are missing."""
    if manifest.document['platform'] != 'windows-x64':
        raise ValueError('Windows assembly requires a Windows release')
    if not manifest.document.get('setup_url') or not manifest.document.get('minimum_os'):
        raise ValueError('Release assembly requires setup_url and minimum_os')
    core = manifest.plan(['core'], 'windows-x64', manifest.document['minimum_os'])
    ids = {item['id'] for item in core}
    # FFmpeg is not shipped: setup downloads gyan.dev's pinned build (install-ffmpeg.ps1).
    required = {'application', 'python', 'postgres-installer', 'postgres-binaries'}
    if not required.issubset(ids):
        raise ValueError('Missing core payloads: ' + ', '.join(sorted(required - ids)))
    roles = {item.get('lock_role') for item in core}
    if not {'core', 'people-view'}.issubset(roles):
        raise ValueError('Core and restored-People view locks are required')
    if 'people' in manifest.document['features']:
        people = manifest.plan(['core', 'people'], 'windows-x64', manifest.document['minimum_os'])
        if not {'insightface', 'facenet'}.issubset({item.get('lock_role') for item in people}):
            raise ValueError('Both selected People provider locks are required')
        for provider in ('insightface', 'facenet'):
            matches = [item for item in people if item.get('model_provider') == provider]
            if len(matches) != 1 or not matches[0].get('model_identity'):
                raise ValueError('Exact model identity required for ' + provider)
    for item in manifest.components.values():
        path = plain_path(payload_root / str(safe_relative(item['bundle_path'])))
        if path.stat().st_size != item['byte_size'] or file_digest(path) != item['sha256']:
            raise ValueError('Release payload differs from manifest: ' + item['id'])
        if not item['license_ids']:
            raise ValueError('Release payload lacks license inventory IDs: ' + item['id'])
    if file_digest(payload_root / manifest.document['agreement_path']) != manifest.document['agreement_sha256']:
        raise ValueError('Release agreement differs from manifest')


def make_manifest(recipe, payload_root):
    doc = json.loads(recipe.read_text(encoding='utf-8'))
    for item in doc['components']:
        local = plain_path(payload_root / str(safe_relative(item['bundle_path'])))
        item['byte_size'] = local.stat().st_size
        item['sha256'] = file_digest(local)
    doc['agreement_sha256'] = file_digest(payload_root / str(safe_relative(doc['agreement_path'])))
    raw = (json.dumps(doc, sort_keys=True, indent=2) + '\n').encode()
    Manifest(raw, sha256(raw))
    return raw


def build_engine(root, manifest, payload_root, destination):
    parsed = Manifest(manifest, sha256(manifest))
    agreement = plain_path(payload_root / parsed.document['agreement_path'])
    if file_digest(agreement) != parsed.document['agreement_sha256']:
        raise ValueError('Agreement changed after manifest creation')
    files = [*root.glob('installer/common/*.py'),
             *root.glob('installer/windows/*.py'),
             root / 'installer/windows/online-dialog.ps1',
             root / 'installer/windows/acquisition-dialog.ps1',
             root / 'installer/windows/setup-run.cs',
             root / 'installer/windows/provision-prepared.ps1',
             root / 'app/migration/progress_lines.py']
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(set(files)):
            archive.write(path, path.relative_to(root).as_posix())
        archive.writestr('installer-manifest.json', manifest)
        name = parsed.document['agreement_path']
        archive.write(agreement, name)


def build_exe(root, descriptor, output):
    doc = json.loads(descriptor.read_text(encoding='utf-8'))
    if doc.get('schema_version') != 1:
        raise ValueError('Unknown bootstrap descriptor')
    if doc.get('mode') not in ('online', 'offline') or not doc.get('bootstrap_notices'):
        raise ValueError('Delivery mode and bootstrap notices are required')
    digest_string(doc['manifest_sha256'])
    for key in ('python', 'engine'):
        digest_string(doc[key]['sha256'])
        safe_relative(doc[key]['bundle_path'])
        if not doc[key]['url'].startswith('https://'):
            raise ValueError('Bootstrap requires HTTPS URLs')
    if output.exists():
        raise ValueError('Output already exists')
    script = base64.b64encode((root / 'installer/windows/bootstrap.ps1').read_bytes()).decode()
    config = base64.b64encode(descriptor.read_bytes()).decode()
    # Values are base64 rather than interpolated executable PowerShell/C# strings.
    source = '''using System;
using System.IO;
using System.Diagnostics;
class Setup {
  static int Main(string[] args) {
    try {
      string backup = null;
      if (args.Length == 2 && args[0] == "--backup") backup = Path.GetFullPath(args[1]);
      else if (args.Length != 0) throw new ArgumentException("Expected --backup PATH, or no arguments.");
      string folder = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "FamilyFoto", "Launch", Guid.NewGuid().ToString("N"));
      Directory.CreateDirectory(folder);
      string script = Path.Combine(folder, "bootstrap.ps1");
      string config = Path.Combine(folder, "bootstrap.json");
      File.WriteAllBytes(script, Convert.FromBase64String("SCRIPT"));
      File.WriteAllBytes(config, Convert.FromBase64String("CONFIG"));
      string shell = Path.Combine(Environment.GetEnvironmentVariable("SystemRoot"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe");
      string bundle = Path.GetDirectoryName(Process.GetCurrentProcess().MainModule.FileName);
      var info = new ProcessStartInfo(shell, "-NoProfile -ExecutionPolicy Bypass -File \\\"" + script + "\\\" -Descriptor \\\"" + config + "\\\" -BundleRoot \\\"" + bundle + "\\\"");
      info.UseShellExecute = false;
      if (backup != null) info.EnvironmentVariables["FAMILYFOTO_SETUP_BACKUP"] = backup;
      var child = Process.Start(info); child.WaitForExit();
      if (child.ExitCode != 0) { Console.Error.WriteLine("Setup stopped. Press Enter to close."); Console.ReadLine(); }
      return child.ExitCode;
    } catch (Exception e) { Console.Error.WriteLine(e.Message); Console.ReadLine(); return 1; }
  }
}
'''.replace('SCRIPT', script).replace('CONFIG', config)
    cs = output.with_suffix('.cs')
    with cs.open('x', encoding='utf-8') as stream:
        stream.write(source)
    compiler = Path(os.environ['SystemRoot']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    subprocess.run([str(compiler), '/nologo', '/target:exe', '/platform:x64',
                    '/out:' + str(output), str(cs)], check=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['manifest', 'engine', 'exe', 'audit'])
    p.add_argument('--input', type=Path, required=True)
    p.add_argument('--payload-root', type=Path)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    root = Path(__file__).resolve().parents[2]
    if a.command == 'audit':
        if not a.payload_root:
            p.error('--payload-root required')
        raw = a.input.read_bytes()
        audit_windows_release(Manifest(raw, sha256(raw)), a.payload_root)
        with a.output.open('x', encoding='utf-8') as stream:
            json.dump({'manifest_sha256': sha256(raw), 'payload_audit': 'passed',
                       'qualified_installation': False}, stream, indent=2)
    elif a.command == 'manifest':
        if not a.payload_root:
            p.error('--payload-root required')
        with a.output.open('xb') as stream:
            stream.write(make_manifest(a.input, a.payload_root))
    elif a.command == 'engine':
        if not a.payload_root:
            p.error('--payload-root required')
        build_engine(root, a.input.read_bytes(), a.payload_root, a.output)
    else:
        build_exe(root, a.input, a.output)


if __name__ == '__main__':
    main()
