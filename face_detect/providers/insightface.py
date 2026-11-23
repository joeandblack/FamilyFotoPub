"""Direct InsightFace model_zoo inference from explicitly supplied ONNX files."""
from face_detect.contracts import ConfigurationError,ProviderUnavailable

class InsightFaceProvider:
    def __init__(self,profile,manifest,assets):
        self.profile,self.manifest,self.assets=profile,manifest,assets
        self.detector=self.recognizer=None

    def load(self):
        if self.detector is not None:return
        try:
            import onnxruntime as ort
            from insightface import model_zoo
        except ImportError:raise ProviderUnavailable('Install the InsightFace provider dependencies') from None
        requested={'cpu':'CPUExecutionProvider','cuda':'CUDAExecutionProvider','coreml':'CoreMLExecutionProvider'}.get(self.profile['device'])
        if requested is None or requested not in ort.get_available_providers():
            raise ConfigurationError('Requested InsightFace execution provider is unavailable')
        options=ort.SessionOptions();options.intra_op_num_threads=2;options.inter_op_num_threads=1
        # No pack names/auto-download: only previously hash-validated local files.
        self.detector=model_zoo.get_model(str(self.assets['detector']),providers=[requested],sess_options=options)
        self.recognizer=model_zoo.get_model(str(self.assets['recognizer']),providers=[requested],sess_options=options)
        if self.detector is None or self.recognizer is None:raise ConfigurationError('Unrecognized InsightFace model architecture')
        settings=self.manifest.get('settings',{})
        size=settings.get('detector_size',640)
        if type(size)is not int or not 128<=size<=2048:raise ConfigurationError('Invalid detector input size')
        ctx=-1 if self.profile['device']=='cpu' else 0
        self.detector.prepare(ctx_id=ctx,input_size=(size,size),det_thresh=settings.get('detection_threshold',.8))
        self.recognizer.prepare(ctx_id=ctx)

    def analyze(self,image,include_embeddings=True):
        self.load()
        import numpy as np
        from insightface.app.common import Face
        bgr=np.asarray(image)[:,:,::-1].copy()
        boxes,points=self.detector.detect(bgr,max_num=100)
        result=[]
        for index,b in enumerate(boxes):
            if points is None:raise ProviderUnavailable('Recognition requires five-point detector landmarks')
            x1,y1,x2,y2=[float(v) for v in b[:4]]
            x1=max(0.,x1);y1=max(0.,y1);x2=min(float(image.width),x2);y2=min(float(image.height),y2)
            if x1>=x2 or y1>=y2:continue
            face=Face(bbox=b[:4],kps=points[index],det_score=float(b[4]))
            row={'box':dict(x_min=x1,y_min=y1,x_max=x2,y_max=y2,probability=float(b[4])),
                 'landmarks':points[index].astype(float).tolist()}
            if include_embeddings:row['vector']=self.recognizer.get(bgr,face).astype(float).reshape(-1).tolist()
            result.append(row)
        return result

    def embed_geometry(self,image,geometry):
        """Vectors from stored five-point landmarks only: no detector runs, so the same
        landmarks give the same alignment whatever found them."""
        self.load()
        import numpy as np
        from insightface.app.common import Face
        bgr=np.asarray(image)[:,:,::-1].copy()
        vectors=[]
        for item in geometry:
            # The detector's own landmarks are float32; this keeps a stored copy identical.
            face=Face(kps=np.asarray(item['landmarks'],dtype=np.float32))
            vectors.append(self.recognizer.get(bgr,face).astype(float).reshape(-1).tolist())
        return vectors

    def close(self):self.detector=self.recognizer=None
