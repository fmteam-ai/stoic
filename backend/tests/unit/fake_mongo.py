"""In-memory async Mongo stand-in for Security & Health Agent unit tests (no live Mongo)."""
import re
from bson import ObjectId


def _match(doc, q):
    for k, v in q.items():
        cur = doc
        for part in k.split("."):
            if isinstance(cur, list):                      # dotted path through an array: any element
                cur = [c.get(part) for c in cur if isinstance(c, dict)]
                cur = max(cur) if cur and all(isinstance(c, str) for c in cur) else (cur or None)
            else:
                cur = cur.get(part) if isinstance(cur, dict) else None
        if isinstance(v, dict):
            for op, arg in v.items():
                if op == "$in" and cur not in arg:
                    return False
                if op == "$nin" and cur in arg:
                    return False
                if op == "$ne" and cur == arg:
                    return False
                if op == "$gte" and (cur is None or cur < arg):
                    return False
                if op == "$gt" and (cur is None or cur <= arg):
                    return False
                if op == "$lt" and (cur is None or cur >= arg):
                    return False
                if op == "$regex" and not re.search(arg, str(cur or "")):
                    return False
        elif cur != v:
            return False
    return True


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def sort(self, *a):
        return self

    def limit(self, n):
        self.rows = self.rows[:n]
        return self

    async def to_list(self, length=None):
        return self.rows

    def __aiter__(self):
        self._it = iter(self.rows)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class _Res:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeCollection:
    def __init__(self):
        self.rows: list[dict] = []
        self.indexes: list = []

    def find(self, q=None, proj=None):
        for r in self.rows:
            r.setdefault("_id", ObjectId())
        return _Cursor([dict(r) for r in self.rows if _match(r, q or {})])

    async def find_one(self, q=None, proj=None, sort=None):
        rows = [r for r in self.rows if _match(r, q or {})]
        return dict(rows[0]) if rows else None

    async def count_documents(self, q):
        return len([r for r in self.rows if _match(r, q)])

    async def insert_one(self, doc):
        d = dict(doc)
        d.setdefault("_id", ObjectId())
        self.rows.append(d)
        return _Res(inserted_id=d["_id"])

    def _apply(self, r, upd):
        for k, v in (upd.get("$set") or {}).items():
            r[k] = v
        for k, v in (upd.get("$inc") or {}).items():
            r[k] = r.get(k, 0) + v
        for k in (upd.get("$unset") or {}):
            r.pop(k, None)
        for k, v in (upd.get("$push") or {}).items():
            arr = r.setdefault(k, [])
            arr.extend(v["$each"] if isinstance(v, dict) and "$each" in v else [v])
            if isinstance(v, dict) and "$slice" in v:
                r[k] = arr[v["$slice"]:]

    async def update_one(self, q, upd, upsert=False):
        for r in self.rows:
            if _match(r, q):
                self._apply(r, upd)
                return _Res(matched_count=1, modified_count=1)
        if upsert:
            d = dict(q)
            self._apply(d, upd)
            d.setdefault("_id", ObjectId())
            self.rows.append(d)
        return _Res(matched_count=0, modified_count=0)

    async def update_many(self, q, upd):
        n = 0
        for r in self.rows:
            if _match(r, q):
                self._apply(r, upd)
                n += 1
        return _Res(matched_count=n, modified_count=n)

    async def create_index(self, *a, **kw):
        self.indexes.append((a, kw))
        return "idx"

    async def index_information(self):
        return {}


class FakeDb:
    def __init__(self):
        self._c: dict[str, FakeCollection] = {}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._c.setdefault(name, FakeCollection())

    def __getitem__(self, name):
        return getattr(self, name)
