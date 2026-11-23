"""Allowlisted lazy imports; configuration cannot name arbitrary Python code."""
import importlib
from .contracts import ConfigurationError

PROVIDERS={'insightface':('face_detect.providers.insightface','InsightFaceProvider'),
           'facenet':('face_detect.providers.facenet','FaceNetProvider')}

def create(profile,manifest,assets):
    if profile.get('python'):
        from .providers.process import ProcessProvider
        return ProcessProvider(profile,manifest,assets)
    try:module,name=PROVIDERS[profile['provider']]
    except KeyError:raise ConfigurationError('Unknown face provider') from None
    return getattr(importlib.import_module(module),name)(profile,manifest,assets)
