"""Bounded inference subprocess for providers with incompatible dependencies."""
import base64
import io
import json
from pathlib import Path
import subprocess
from face_detect.contracts import ConfigurationError,ProviderUnavailable

class ProcessProvider:
    def __init__(self,profile,manifest,assets):
        self.profile,self.manifest,self.assets=profile,manifest,assets
        self.python=Path(profile['python'])
        if not self.python.is_absolute() or not self.python.is_file():
            raise ConfigurationError('Provider Python executable is missing')
        self.root=Path(__file__).resolve().parents[2]

    def versions(self):
        try:
            result=subprocess.run([str(self.python),'-m','face_detect.providers.runner','versions',self.profile['provider']],
                cwd=self.root,capture_output=True,text=True,encoding='utf-8',timeout=15,check=True,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            return json.loads(result.stdout)
        except (OSError,subprocess.SubprocessError,ValueError):
            raise ProviderUnavailable('Provider dependencies are unavailable; run native provider setup and doctor') from None

    def analyze(self,image,include_embeddings=True):
        return self._request('analyze',image,include_embeddings=include_embeddings)

    def embed_geometry(self,image,geometry):
        return self._request('embed',image,geometry=geometry)

    def _request(self,action,image,**values):
        buffer=io.BytesIO();image.save(buffer,format='PNG')
        request={'profile':{k:v for k,v in self.profile.items() if k!='python'},'manifest':self.manifest,
                 'assets':{k:str(v) for k,v in self.assets.items()},'image':base64.b64encode(buffer.getvalue()).decode(),
                 **values}
        try:
            result=subprocess.run([str(self.python),'-m','face_detect.providers.runner',action],cwd=self.root,
                input=json.dumps(request),capture_output=True,text=True,encoding='utf-8',timeout=120,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        except subprocess.TimeoutExpired:raise ProviderUnavailable('Native inference exceeded 120 seconds; job paused') from None
        except (OSError,UnicodeError):raise ProviderUnavailable('Native inference worker could not start or return a valid response') from None
        if result.returncode or len(result.stdout)>4*1024*1024:
            raise ProviderUnavailable('Native inference failed; run face_detect doctor for dependency/model diagnostics')
        try:return json.loads(result.stdout)
        except ValueError:raise ProviderUnavailable('Invalid inference worker response') from None

    def close(self):pass
