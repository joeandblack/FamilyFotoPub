"""Check configured local assets and optional explicit test image; never scans a library."""
import argparse
import json
import time
from app.config import load_config
from face_detect.service import create_service

def main():
    p=argparse.ArgumentParser();p.add_argument('--profile');p.add_argument('--image');a=p.parse_args()
    cfg=load_config()
    if a.profile:cfg['face_detection']['active_profile']=a.profile
    service=create_service(cfg);service.observe()
    result={'provider':service.profile['provider'],'model_id':service.identity,'assets':'verified','matching':service.manifest['matching']}
    if a.image:
        from face_detect.jobs.automatic_people_scan import decode_canonical
        image,_=decode_canonical(a.image);start=time.monotonic();faces=service.detect(image)
        result.update(faces=len(faces),dimensions=[len(f['vector']) for f in faces],seconds=round(time.monotonic()-start,2),unit_vectors=all(abs(sum(x*x for x in f['vector'])-1)<1e-6 for f in faces))
    print(json.dumps(result));service.close()

if __name__=='__main__':main()
