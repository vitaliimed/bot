"""In-memory transactional test double, including merge semantics and thread exclusion."""
import copy
import threading
from types import SimpleNamespace

class Database:
    def __init__(self):
        self.data = {}
        self.lock = threading.RLock()

    def collection(self, name): return Collection(self, name)
    def transaction(self): return self
    def transactional(self, function):
        def run(tx):
            with self.lock: return function(tx)
        return run
    def update(self, ref, value): ref.update(value)
    def set(self, ref, value): ref.set(value)
    def delete(self, ref): ref.delete()

class Reference:
    def __init__(self, db, collection, id):
        self.db, self.collection, self.id = db, collection, id
        self.key = (collection, id)
    def get(self, transaction=None):
        data = copy.deepcopy(self.db.data.get(self.key))
        return SimpleNamespace(id=self.id, reference=self, exists=data is not None, to_dict=lambda:data)
    def create(self, value):
        from google.api_core.exceptions import AlreadyExists
        with self.db.lock:
            if self.key in self.db.data: raise AlreadyExists('exists')
            self.set(value)
    def set(self, value, merge=False):
        if not merge: self.db.data[self.key] = copy.deepcopy(value)
        else:
            target = self.db.data.setdefault(self.key,{})
            for key, val in value.items():
                if isinstance(val,dict): target.setdefault(key,{}).update(copy.deepcopy(val))
                else: target[key] = copy.deepcopy(val)
    def update(self, value):
        from google.cloud import firestore
        target = self.db.data[self.key]
        for key, val in value.items():
            if val is firestore.DELETE_FIELD: target.pop(key,None)
            else: target[key] = copy.deepcopy(val)
    def delete(self): self.db.data.pop(self.key,None)

class Collection:
    def __init__(self, db, name, filters=(), maximum=None, fields=None):
        self.db,self.name,self.filters,self.maximum,self.fields = db,name,filters,maximum,fields
    def document(self,id): return Reference(self.db,self.name,id)
    def select(self,fields): return Collection(self.db,self.name,self.filters,self.maximum,fields)
    def where(self,key,op,value): return Collection(self.db,self.name,(*self.filters,(key,value)),self.maximum,self.fields)
    def limit(self,n): return Collection(self.db,self.name,self.filters,n,self.fields)
    def stream(self):
        rows = [self.document(id).get() for (name,id),data in list(self.db.data.items())
                if name == self.name and all(data.get(k) == v for k,v in self.filters)]
        for row in rows[:self.maximum]:
            if self.fields is not None:
                data={k:v for k,v in row.to_dict().items() if k in self.fields}
                row=SimpleNamespace(id=row.id,reference=row.reference,exists=row.exists,to_dict=lambda data=data:copy.deepcopy(data))
            yield row
