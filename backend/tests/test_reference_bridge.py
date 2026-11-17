"""桥接/持久化语义测试：用内存 ORM 替身验证 reference_store 的落库、
整链回滚、重启动重建与快照冻结（本机无 SQLAlchemy/fastapi，由 _shims 兜底）。
"""
import threading
import unittest

from tests import _shims
_shims.install()

from tests.fake_orm import FakeDB, FakeRow  # noqa: E402
from src.services.references import FD, ReferenceError  # noqa: E402
from src.services import reference_store  # noqa: E402
from src.services import dependencies as dep_svc  # noqa: E402
from src.models import entities  # noqa: E402


def _mark_fake_model(cls, pk, columns):
    cls._fake_pk = pk
    cls._fake_columns = columns


_mark_fake_model(entities.Relation, "id", {"note": "", "revision": 0})
_mark_fake_model(entities.Dependency, "id", {"note": ""})
_mark_fake_model(entities.Reference, "id", {
    "status": "pending", "conflict_reason": None,
    "applied_source_revision": None, "committed_derived": list,
})
_mark_fake_model(entities.Setting, "key", {})
_mark_fake_model(entities.Audit, "id", {})


class _User:
    email = "operator@nanlu.test"


class _RefInput:
    def __init__(self, source, target, mapping):
        self.source_relation_id = source
        self.target_relation_id = target
        self.mapping = mapping


class BridgePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        for cls in (entities.Relation, entities.Dependency, entities.Reference,
                    entities.Setting, entities.Audit):
            self.db.register(cls)
        self.db.add(FakeRow(entities.Relation, id=1, code="SRC", title="源",
                            attributes=["A", "B", "C"], note="", revision=0))
        self.db.add(FakeRow(entities.Relation, id=2, code="TGT", title="目标",
                            attributes=["X", "Y", "Z"], note="", revision=0))
        self.db.checkpoint()
        reference_store.reset_engine()
        self.user = _User()

    def _engine(self):
        return reference_store.get_engine(self.db)

    def _add_own(self, relation_id, lhs, rhs):
        row = FakeRow(entities.Dependency, relation_id=relation_id,
                      lhs=list(lhs), rhs=list(rhs), note="自有")
        self.db.add(row)
        self.db.flush()
        dep_svc._sync_engine(self.db, row)
        return row

    def test_hydrate_rebuilds_engine_after_restart(self):
        self._add_own(1, ["A"], ["B"])
        ref = reference_store.create_reference(
            self.db, _RefInput(1, 2, {"A": "X", "B": "Y"}), self.user)
        reference_store.propagate(self.db, 1, self.user)
        self.db.checkpoint()
        reference_store.reset_engine()  # 模拟进程重启
        engine = reference_store.get_engine(self.db)
        self.assertEqual(engine.references[ref["id"]].status, "effective")
        view = engine.effective_view(2)
        self.assertIn({"lhs": ["X"], "rhs": "Y"}, [r["fd"] for r in view["rules"]])
        self.assertEqual(engine.revisions[1], self.db.get(entities.Relation, 1).revision)
        # 已提交派生规则也从持久层还原
        self.assertTrue(engine.committed_derived[2][ref["id"]])

    def test_propagation_failure_rolls_back_db_and_engine(self):
        self._add_own(1, ["A"], ["B"])
        reference_store.create_reference(
            self.db, _RefInput(1, 2, {"A": "X", "B": "Y"}), self.user)
        reference_store.propagate(self.db, 1, self.user)
        self.db.checkpoint()
        before = self.db.snapshot_committed()
        engine = self._engine()
        engine.fail_before_relation = 2
        self._add_own(1, ["A"], ["C"])  # 触发版本提升
        with self.assertRaises(ReferenceError):
            reference_store.propagate_uncommitted(self.db, 1)
        self.db.rollback()
        engine.fail_before_relation = None
        after = self.db.snapshot_committed()
        self.assertEqual(set(before[entities.Reference]), set(after[entities.Reference]))
        ref_id = next(iter(before[entities.Reference]))
        self.assertEqual(after[entities.Reference][ref_id]["status"], "effective")
        view = engine.effective_view(2)
        self.assertIn({"lhs": ["X"], "rhs": "Y"}, [r["fd"] for r in view["rules"]])

    def test_snapshot_consistent_all_old_or_all_new(self):
        self._add_own(1, ["A"], ["B"])
        reference_store.create_reference(
            self.db, _RefInput(1, 2, {"A": "X", "B": "Y"}), self.user)
        reference_store.propagate(self.db, 1, self.user)
        self.db.checkpoint()
        old = reference_store.freeze_snapshot(self.db, 2)
        engine = self._engine()
        rule = engine.own_rules[1][0]
        engine.correct_own_rule(1, rule.id, FD.make(["A"], "C"))
        ref = next(iter(engine.references.values()))
        ref.mapping = {"A": "X", "C": "Z"}
        ref.status = "pending"
        ref.applied_source_revision = None
        reference_store.propagate(self.db, 1, self.user)
        self.db.checkpoint()
        new = reference_store.freeze_snapshot(self.db, 2)
        self.assertEqual({d["rhs"][0] for d in old["dependencies"]}, {"Y"})
        self.assertEqual({d["rhs"][0] for d in new["dependencies"]}, {"Z"})
        self.assertGreater(new["global_revision"], old["global_revision"])

    def test_concurrent_report_and_correction_never_mix(self):
        self._add_own(1, ["A"], ["B"])
        reference_store.create_reference(
            self.db, _RefInput(1, 2, {"A": "X", "B": "Y"}), self.user)
        reference_store.propagate(self.db, 1, self.user)
        self.db.checkpoint()
        errors, snapshots = [], []

        def correct():
            try:
                engine = self._engine()
                with engine._lock:
                    rule = engine.own_rules[1][0]
                    engine.correct_own_rule(1, rule.id, FD.make(["A"], "C"))
                    ref = next(iter(engine.references.values()))
                    ref.mapping = {"A": "X", "C": "Z"}
                    ref.status = "pending"
                    ref.applied_source_revision = None
                reference_store.propagate(self.db, 1, self.user)
                self.db.checkpoint()
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        def accept():
            try:
                snapshots.append(reference_store.freeze_snapshot(self.db, 2))
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=correct if i % 2 == 0 else accept)
                   for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertTrue(snapshots)
        for snap in snapshots:
            rhs = {d["rhs"][0] for d in snap["dependencies"]}
            self.assertTrue(rhs <= {"Y", "Z"})
            self.assertFalse({"Y", "Z"} <= rhs, "快照混用了新旧依赖")


if __name__ == "__main__":
    unittest.main(verbosity=2)
