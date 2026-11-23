"""Bounded worker-only cache of versioned vectors, never review decisions."""
from array import array
from collections import OrderedDict


class EmbeddingCache:
    def __init__(self,max_bytes=256*1024*1024):
        self.max_bytes=max_bytes
        self.scope=None
        self.entries=OrderedDict()
        self.bytes=0

    def records(self,db,model):
        identity=db.execute('SELECT current_database() db,current_schema() schema,inet_server_addr()::text host,inet_server_port() port').fetchone()
        scope=(*identity.values(),model)
        if scope!=self.scope:
            self.scope=scope;self.entries.clear();self.bytes=0
        records=db.execute('''SELECT o.*,e.xmin::text||':'||e.ctid::text vector_version
          FROM auto_people_observations o JOIN embedding e USING(detection_id,space_id)
          WHERE o.space_id=%s ORDER BY detection_id''',(model,)).fetchall()
        wanted={(r['detection_id'],r['vector_version']) for r in records}
        for key in list(self.entries):
            if key not in wanted:self.bytes-=len(self.entries.pop(key))*8
        missing=[r['detection_id'] for r in records if (r['detection_id'],r['vector_version']) not in self.entries]
        available=dict(self.entries)
        fetched={}
        for offset in range(0,len(missing),250):
            for row in db.execute('''SELECT detection_id,vector,xmin::text||':'||ctid::text vector_version
              FROM embedding WHERE space_id=%s AND detection_id=ANY(%s)''',
              (model,missing[offset:offset+250])).fetchall():
                vector=array('d',row['vector'])
                fetched[row['detection_id']]=(row['vector_version'],vector)
        result=[]
        for row in records:
            did=row['detection_id'];key=(did,row['vector_version'])
            vector=available.get(key)
            if vector is None:
                current=fetched.get(did)
                if current is None:continue  # Removed since the metadata query.
                version,vector=current;key=(did,version)
            if key not in self.entries:
                size=len(vector)*8
                if size<=self.max_bytes:
                    while self.entries and self.bytes+size>self.max_bytes:
                        _,old=self.entries.popitem(last=False);self.bytes-=len(old)*8
                    self.entries[key]=vector;self.bytes+=size
            else:self.entries.move_to_end(key)
            result.append({**row,'vector':vector})
        return result
