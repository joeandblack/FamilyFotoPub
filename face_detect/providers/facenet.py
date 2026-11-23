"""Direct facenet-pytorch MTCNN/InceptionResnetV1; no pretrained downloads."""
from face_detect.contracts import ConfigurationError,ProviderUnavailable

class FaceNetProvider:
    def __init__(self,profile,manifest,assets):
        self.profile,self.manifest,self.assets=profile,manifest,assets
        self.detector=self.recognizer=None

    def load(self):
        if self.detector is not None:return
        try:
            import torch
            from facenet_pytorch import MTCNN,InceptionResnetV1
        except ImportError:raise ProviderUnavailable('Install the FaceNet provider dependencies') from None
        device=self.profile['device']
        if device not in ('cpu','cuda','mps') or device=='cuda' and not torch.cuda.is_available() or device=='mps' and not torch.backends.mps.is_available():
            raise ConfigurationError('Requested FaceNet device is unavailable')
        torch.set_num_threads(2)
        self.detector=MTCNN(keep_all=True,device=device,post_process=True)
        # Override all bundled detector weights with the approved, pinned assets.
        for name in ('pnet','rnet','onet'):
            getattr(self.detector,name).load_state_dict(torch.load(self.assets[name],map_location='cpu',weights_only=True),strict=True)
        self.recognizer=InceptionResnetV1(pretrained=None,classify=False).eval()
        weights=torch.load(self.assets['recognizer'],map_location='cpu',weights_only=True)
        # Some published checkpoints include training classifier weights.
        classes=weights.get('logits.weight')
        if classes is not None:
            self.recognizer.logits=torch.nn.Linear(512,classes.shape[0])
        self.recognizer.load_state_dict(weights,strict=True)
        self.recognizer.to(device)

    def analyze(self,image,include_embeddings=True):
        self.load()
        import torch
        with torch.inference_mode():
            boxes,scores,points=self.detector.detect(image,landmarks=True)
            if boxes is None:return []
            indices=[i for i,p in enumerate(scores) if p>=self.manifest.get('settings',{}).get('detection_threshold',.8)]
            if len(indices)>100:raise ProviderUnavailable('Too many faces for bounded inference')
            vectors=None
            if include_embeddings and indices:
                crops=self.detector.extract(image,boxes[indices],save_path=None)
                vectors=self.recognizer(crops.to(self.profile['device'])).cpu().numpy()
            result=[]
            for n,i in enumerate(indices):
                b=boxes[i];x1,y1,x2,y2=max(0.,float(b[0])),max(0.,float(b[1])),min(float(image.width),float(b[2])),min(float(image.height),float(b[3]))
                if x1>=x2 or y1>=y2:continue
                row={'box':dict(x_min=x1,y_min=y1,x_max=x2,y_max=y2,probability=float(scores[i])),
                     'landmarks':points[i].astype(float).tolist()}
                if include_embeddings:row['vector']=vectors[n].astype(float).tolist()
                result.append(row)
            return result

    def embed_geometry(self,image,geometry):
        """Vectors from stored boxes only, cropped the way analyze crops them."""
        self.load()
        import numpy as np
        import torch
        if not geometry:return []
        boxes=np.asarray([item['box'] for item in geometry],dtype=np.float32)
        with torch.inference_mode():
            crops=self.detector.extract(image,boxes,save_path=None)
            return self.recognizer(crops.to(self.profile['device'])).cpu().numpy().astype(float).tolist()

    def close(self):self.detector=self.recognizer=None
