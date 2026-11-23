"""Deterministic in-process provider for queue and receipt integration tests."""
import copy
import hashlib
import json

from face_detect.service import FaceService


class FixtureProvider(FaceService):
    def __init__(self, transport=None, identity_seed='fixture', preprocessing='crop'):
        self._transport = transport or self.faces
        self.manifest = {'schema': 'familyfoto-test-model-v1', 'provider': 'synthetic',
                         'identity_seed': identity_seed, 'preprocessing': preprocessing,
                         'dimensions': 512}
        self.identity = hashlib.sha256(json.dumps(self.manifest, sort_keys=True).encode()).hexdigest()
        # A synthetic provider defines its own space and detector, named after its identity.
        self.space_id = 'synthetic-space-' + self.identity[:24]
        self.detector_id = 'synthetic-detector-' + self.identity[:24]
        self.assets = {}

    @staticmethod
    def faces(image):
        width, height = image.size
        return [{'box': {'x_min': 0, 'y_min': 0, 'x_max': width, 'y_max': height,
                         'probability': .99},
                 'landmarks': [[width*x, height*y] for x, y in
                               ((.2, .3), (.8, .3), (.5, .5), (.3, .8), (.7, .8))],
                 'vector': [1.] + [0.]*511}]

    def detect(self, image):
        return [dict(copy.deepcopy(face), model_id=self.identity, space_id=self.space_id,
                     detector_id=self.detector_id) for face in self._transport(image)]
