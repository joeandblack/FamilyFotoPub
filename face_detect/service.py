"""The only public inference entry point; immutable model space and provenance."""
import hashlib
from importlib.metadata import version,PackageNotFoundError
import json
import math
from . import configuration,registry
from .contracts import ProviderUnavailable,validate_faces,validate_geometry,validate_vectors
from .recognition.quality import quality_seed

class FaceService:
    capabilities=frozenset(('detection','landmarks','embeddings'))
    def __init__(self,profile,manifest,assets,backend):
        self.profile,self.assets,self.backend=profile,assets,backend
        dependencies=('insightface','onnxruntime') if profile['provider']=='insightface' else ('facenet-pytorch','torch')
        versions={}
        for name in dependencies:
            try:versions[name]=version(name)
            except PackageNotFoundError:versions[name]='not-installed'
        if hasattr(backend,'versions'):versions=backend.versions()
        self.manifest=configuration.identity_manifest(manifest,profile,versions)
        # The exact model on this computer: it pins a running scan to its inputs.
        self.identity=hashlib.sha256(json.dumps(self.manifest,sort_keys=True,allow_nan=False).encode()).hexdigest()
        # What the People catalog keys by: the space vectors belong to, and where boxes came
        # from. Neither changes with the computer or its library versions.
        # A provider with no defined space (a test double) has None, and can't write to a catalog.
        from .contracts import ConfigurationError
        from .spaces.identity import detector_id,space_id
        try:self.space_id=space_id(self.manifest)
        except ConfigurationError:self.space_id=None
        try:self.detector_id=detector_id(self.manifest)
        except ConfigurationError:self.detector_id=None

    def observe(self):
        for name,path in self.assets.items():
            with path.open('rb') as stream:actual=hashlib.file_digest(stream,'sha256').hexdigest()
            if actual!=self.manifest['assets'][name]['sha256']:raise ProviderUnavailable('Model asset changed; job paused')
        return {'stamp':{'model_id':self.identity},'evidence':{'kind':'native-model-v1','manifest':self.manifest}}

    def validate(self,stamp):
        if stamp!={'model_id':self.identity}:raise ProviderUnavailable('Job model identity differs')
        return self

    def verify(self,record):
        if record!=self.observe():raise ProviderUnavailable('Native model provenance changed')

    def analyze(self,image,include_embeddings=True):
        if image.mode!='RGB' or max(image.size)>6400 or image.width*image.height>24_000_000:
            raise ProviderUnavailable('Bounded canonical RGB input required')
        before=self.observe()
        result=validate_faces(self.backend.analyze(image,include_embeddings),image.size,self.manifest['dimensions'],include_embeddings)
        if self.observe()!=before:raise ProviderUnavailable('Model changed during inference')
        return [dict(row,model_id=self.identity,space_id=self.space_id,detector_id=self.detector_id) for row in result]

    def detect(self,image):return self.analyze(image,True)

    def embed_result(self,image,target_box):
        if len(target_box)!=4 or not all(isinstance(v,(int,float)) and math.isfinite(v) for v in target_box) or not 0<=target_box[0]<target_box[2]<=image.width or not 0<=target_box[1]<target_box[3]<=image.height:
            raise ProviderUnavailable('Invalid target bounds')
        def overlap(face):
            b=face['box'];a=[b[k] for k in ('x_min','y_min','x_max','y_max')];t=target_box
            inter=max(0,min(a[2],t[2])-max(a[0],t[0]))*max(0,min(a[3],t[3])-max(a[1],t[1]))
            return inter/((a[2]-a[0])*(a[3]-a[1])+(t[2]-t[0])*(t[3]-t[1])-inter)
        ranked=sorted(((overlap(f),i,f) for i,f in enumerate(self.detect(image))),reverse=True)
        if not ranked or ranked[0][0]<.4 or len(ranked)>1 and ranked[0][0]-ranked[1][0]<.1:
            raise ProviderUnavailable('No unambiguous face at target')
        face=ranked[0][2]
        return face|{'seed':quality_seed(face),'quality_policy':'geometry-seed-v1','source_size':list(image.size)}

    def embed(self,image,target_box):return self.embed_result(image,target_box)['vector']

    def describe(self):
        """Space, detector and backend identities. Only the space keys vectors."""
        from .spaces.identity import describe
        return describe(self.manifest)

    def embed_geometry(self,image,geometry):
        """Unit vectors from stored face geometry, in canonical image pixels. No detection.

        Each item is {'landmarks': five [x, y]} or {'box': [x1, y1, x2, y2]}, whichever the
        space aligns by. This is what a backend proves in conformance, and what a new space
        uses to re-embed stored faces."""
        from .spaces.identity import space_spec
        if image.mode!='RGB' or max(image.size)>6400 or image.width*image.height>24_000_000:
            raise ProviderUnavailable('Bounded canonical RGB input required')
        kind=space_spec(self.manifest)['alignment']['kind']
        geometry=validate_geometry(geometry,image.size,kind)
        if not hasattr(self.backend,'embed_geometry'):
            raise ProviderUnavailable('This provider cannot embed stored geometry')
        before=self.observe()
        vectors=self.backend.embed_geometry(image,geometry)
        if self.observe()!=before:raise ProviderUnavailable('Model changed during inference')
        return validate_vectors(vectors,len(geometry),self.manifest['dimensions'])
    def close(self):self.backend.close()


def create_service(config,root=None,platform=None):
    if root is None:
        from app.config import ROOT
        root=ROOT
    _,profile=configuration.resolve(config,platform)
    manifest,assets=configuration.read_manifest(profile,root)
    return FaceService(profile,manifest,assets,registry.create(profile,manifest,assets))
