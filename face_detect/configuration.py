"""Explicit profiles and checksum-pinned local assets. No downloads."""
import copy
import hashlib
import json
from pathlib import Path
import sys
from .contracts import ConfigurationError

PREPROCESSING='canonical-primary-srgb-max4096-lanczos-native-v1'


def resolve(config, platform=None):
    cfg=config.get('face_detection',{})
    name=cfg.get('active_profile') or cfg.get('platform_defaults',{}).get(platform or sys.platform)
    if not name or name not in cfg.get('profiles',{}):
        raise ConfigurationError('Select a face_detection profile in config/local.json')
    profile=copy.deepcopy(cfg['profiles'][name])
    if profile.get('provider') not in ('insightface','facenet'):
        raise ConfigurationError('Unknown face provider; choose insightface or facenet')
    if profile.get('device','cpu') not in ('cpu','cuda','coreml','mps'):
        raise ConfigurationError('Unknown inference device')
    profile.setdefault('device','cpu')
    return name,profile


def read_manifest(profile, root):
    path=Path(profile.get('model_manifest',''))
    if not path.is_absolute():path=Path(root)/'face_detect/config'/path
    try:manifest=json.loads(path.read_text())
    except (OSError,ValueError):raise ConfigurationError('Model manifest missing or invalid; provision approved local weights first') from None
    if manifest.get('schema')!='familyfoto-native-model-v1' or manifest.get('provider')!=profile['provider']:
        raise ConfigurationError('Model manifest provider/schema mismatch')
    if manifest.get('dimensions')!=512 or manifest.get('normalization')!='l2':
        raise ConfigurationError('Current People workflows require 512-dimensional L2 embeddings')
    if not isinstance(manifest.get('license'),dict) or not manifest['license'].get('approved') or not manifest['license'].get('source'):
        raise ConfigurationError('Record the model usage approval and source in its manifest')
    settings=manifest.get('settings',{})
    if not 0<float(settings.get('detection_threshold',.8))<1:
        raise ConfigurationError('Invalid detection threshold')
    # Matching policy must be explicitly qualified; never reuse another model threshold.
    policy=manifest.get('matching',{})
    if not isinstance(policy,dict):
        raise ConfigurationError('A model-specific matching policy is required')
    if policy.get('calibrated') is True:
        for key in ('threshold','margin'):
            if type(policy.get(key)) not in (int,float) or not 0<=policy[key]<=1:
                raise ConfigurationError('Invalid calibrated matching policy')
    elif policy.get('mode') != 'review-only':
        raise ConfigurationError('Uncalibrated models must use review-only grouping')
    assets={}
    for name,asset in manifest.get('assets',{}).items():
        p=Path(asset['path'])
        if not p.is_absolute():p=path.parent/p
        if not p.is_file() or p.is_symlink():raise ConfigurationError('Model asset missing or linked: '+name)
        with p.open('rb') as stream:actual=hashlib.file_digest(stream,'sha256').hexdigest()
        if actual!=asset.get('sha256'):raise ConfigurationError('Model checksum mismatch: '+name)
        assets[name]=p.resolve()
    required={'detector','recognizer'} if profile['provider']=='insightface' else {'recognizer','pnet','rnet','onet'}
    if set(assets)!=required:raise ConfigurationError('Incorrect model assets for selected provider')
    return manifest,assets


def identity_manifest(manifest,profile,versions):
    # Physical installation paths do not define an embedding space.
    value=copy.deepcopy(manifest)
    value['assets']={name:{'sha256':asset['sha256']} for name,asset in value['assets'].items()}
    value.update(preprocessing=PREPROCESSING,device=profile['device'],runtime_versions=versions)
    return value
