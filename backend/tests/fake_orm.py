"""内存 ORM 替身：实现 reference_store/dependencies 实际使用的 SQLAlchemy 接口。"""


class FakeRow:
    def __init__(self, model_cls, **kw):
        self._model = model_cls
        for key, value in kw.items():
            setattr(self, key, value)
        for col, default in getattr(model_cls, "_fake_columns", {}).items():
            if not hasattr(self, col):
                setattr(self, col, default() if callable(default) else default)


class FakeTable:
    def __init__(self, model_cls):
        self.model_cls = model_cls
        self.rows = {}
        self._pk_seq = 0

    def insert(self, row):
        pk = self.model_cls._fake_pk
        ident = getattr(row, pk, None)
        if ident is None:
            self._pk_seq += 1
            ident = self._pk_seq
            setattr(row, pk, ident)
        self.rows[ident] = row
        return ident

    def get(self, ident):
        return self.rows.get(ident)


class FakeSelect:
    def __init__(self, model_cls):
        self.model_cls = model_cls
        self._where = None

    def where(self, cond):
        self._where = cond
        return self

    def order_by(self, col):
        return self

    def select_from(self, model_cls):
        self.model_cls = model_cls
        return self


class FakeScalars:
    def __init__(self, rows):
        self._rows = list(rows)

    def __iter__(self):
        return iter(self._rows)

    def all(self):
        return list(self._rows)


def fake_where(attr, value):
    return (attr, value)


class FakeDB:
    """模拟 Session：已提交表 + 暂存；rollback 恢复到上一个 checkpoint。"""

    def __init__(self):
        self.tables = {}
        self._pending = []
        self._committed_snapshot = None

    def register(self, model_cls):
        self.tables.setdefault(model_cls, FakeTable(model_cls))

    def add(self, row):
        self._pending.append(row)

    def flush(self):
        for row in self._pending:
            self.tables[row._model].insert(row)
        self._pending.clear()

    def commit(self):
        self.flush()
        self.checkpoint()

    def checkpoint(self):
        import copy
        self.flush()
        self._committed_snapshot = {
            cls: copy.deepcopy({i: vars(r) for i, r in t.rows.items()})
            for cls, t in self.tables.items()
        }

    def rollback(self):
        self._pending.clear()
        import copy
        if self._committed_snapshot is None:
            for table in self.tables.values():
                table.rows.clear()
            return
        for cls, data in self._committed_snapshot.items():
            self.tables[cls].rows = {
                i: FakeRow(cls, **copy.deepcopy(values)) for i, values in data.items()
            }

    def get(self, model_cls, ident):
        return self.tables[model_cls].get(ident)

    def scalar(self, select_obj):
        return len(self._eval(select_obj))

    def scalars(self, select_obj):
        return FakeScalars(self._eval(select_obj))

    def _eval(self, select_obj):
        rows = list(self.tables[select_obj.model_cls].rows.values())
        if select_obj._where is not None:
            attr, expected = select_obj._where
            rows = [r for r in rows if getattr(r, attr, None) == expected]
        return rows

    def snapshot_committed(self):
        import copy
        return {cls: copy.deepcopy({i: vars(r) for i, r in t.rows.items()})
                for cls, t in self.tables.items()}
