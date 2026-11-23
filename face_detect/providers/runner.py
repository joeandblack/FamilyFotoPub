"""Private stdin/stdout inference protocol. No database, filesystem media writes or HTTP."""
import base64
import contextlib
import hashlib
from importlib.metadata import version
import io
import json
from pathlib import Path
import sys


def main():
    if sys.argv[1]=='versions':
        names=('insightface','onnxruntime') if sys.argv[2]=='insightface' else ('facenet-pytorch','torch')
        print(json.dumps({name:version(name) for name in names}));return
    raw=sys.stdin.read(40*1024*1024+1)
    if len(raw)>40*1024*1024:raise ValueError('Input exceeds bound')
    request=json.loads(raw)
    from PIL import Image
    from face_detect.registry import create
    assets={k:Path(v) for k,v in request['assets'].items()}
    for name,p in assets.items():
        with p.open('rb') as f:actual=hashlib.file_digest(f,'sha256').hexdigest()
        if actual!=request['manifest']['assets'][name]['sha256']:raise ValueError('Model hash mismatch')
    with Image.open(io.BytesIO(base64.b64decode(request['image'],validate=True))) as decoded:
        if max(decoded.size)>6400 or decoded.width*decoded.height>24_000_000:raise ValueError('Image exceeds bound')
        image=decoded.convert('RGB')
    with contextlib.redirect_stdout(sys.stderr):
        backend=create(request['profile'],request['manifest'],assets)
        try:
            if sys.argv[1]=='embed':
                result=backend.embed_geometry(image,request['geometry'])
            elif sys.argv[1]=='analyze':
                result=backend.analyze(image,request['include_embeddings'])
            else:raise ValueError('Unknown worker action')
        finally:backend.close()
    print(json.dumps(result,allow_nan=False))

if __name__=='__main__':
    try:main()
    except Exception as error:
        print(type(error).__name__+': '+str(error),file=sys.stderr)
        sys.exit(1)
