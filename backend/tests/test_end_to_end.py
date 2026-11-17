"""端到端服务流程：依赖写入即触发引用链传播，更正沿链更新，整链回滚。

走 dependencies + reference_store 真实服务函数，仅数据库用内存替身。
"""
import unittest

from tests import _shims
_shims.install()

from tests.fake_orm import FakeDB, FakeRow  # noqa: E402
from src.services import dependencies as dep_svc  # noqa: E402
from src.services import reference_store  # noqa: E402
from src.services.references import ReferenceError  # noqa: E402
from src.models import entities  # noqa: E402


def _mark(cls, pk, columns):
    cls._fake_pk = pk
    cls._fake_columns = columns


_mark(entities.Relation, "id", {"note": "", "revision": 0})
_mark(entities.Dependency, "id", {"note": ""})
_mark(entities.Reference, "id", {
    "status": "pending", "conflict_reason": None,
    "applied_source_revision": None, "committed_derived": list})
_mark(entities.Setting, "key", {})
_mark(entities.Audit, "id", {})


class _User:
    email = "operator@nanlu.test"


class _DepIn:
    def __init__(self, relation_id, lhs, rhs, note=""):
        self.relation_id = relation_id
        self.lhs = lhs
        self.rhs = rhs
        self.note = note

    def model_dump(self):
        return {"relation_id": self.relation_id, "lhs": self.lhs,
                "rhs": self.rhs, "note": self.note}


class _RefIn:
    def __init__(self, s, t, mapping):
        self.source_relation_id = s
        self.target_relation_id = t
        self.mapping = mapping


class EndToEndServiceTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        for cls in (entities.Relation, entities.Dependency, entities.Reference,
                    entities.Setting, entities.Audit):
            self.db.register(cls)
        self.db.add(FakeRow(entities.Relation, id=1, code="SRC", title="源",
                            attributes=["A", "B", "C"], note="", revision=0))
        self.db.add(FakeRow(entities.Relation, id=2, code="MID", title="中游",
                            attributes=["X", "Y", "Z"], note="", revision=0))
        self.db.add(FakeRow(entities.Relation, id=3, code="END", title="末端",
                            attributes=["P", "Q", "R"], note="", revision=0))
        self.db.checkpoint()
        reference_store.reset_engine()
        self.user = _User()

    def test_dependency_create_propagates_through_chain(self):
        # 三级引用链 1->2->3
        reference_store.create_reference(
            self.db, _RefIn(1, 2, {"A": "X", "B": "Y"}), self.user)
        reference_store.create_reference(
            self.db, _RefIn(2, 3, {"X": "P", "Y": "Q"}), self.user)
        # 通过真实依赖服务写入自有规则，应自动传播整条链
        out = dep_svc.create(self.db, _DepIn(1, ["A"], ["B"]), self.user)
        self.assertEqual(out["reference_propagation"]["targets"], [2, 3])
        view3 = reference_store.effective_view(self.db, 3)
        self.assertIn({"lhs": ["P"], "rhs": "Q"}, [r["fd"] for r in view3["rules"]])
        # 末端规则带完整推导来源
        derived = [r for r in view3["rules"] if r["kind"] == "derived"]
        self.assertTrue(derived)

    def test_dependency_correction_updates_chain_and_keeps_own_rules(self):
        reference_store.create_reference(
            self.db, _RefIn(1, 2, {"A": "X", "B": "Y", "C": "Z"}), self.user)
        reference_store.create_reference(
            self.db, _RefIn(2, 3, {"X": "P", "Y": "Q", "Z": "R"}), self.user)
        # 末端有一条自有规则，传播不得改动它
        dep_svc.create(self.db, _DepIn(3, ["P"], ["R"], note="末端自有"), self.user)
        dep_svc.create(self.db, _DepIn(1, ["A"], ["B"]), self.user)
        dep_id = next(i for i, row in self.db.tables[entities.Dependency].rows.items()
                      if row.relation_id == 1 and row.lhs == ["A"])
        # 更正源规则 A->B 为 A->C
        dep_svc.update(self.db, dep_id, _DepIn(1, ["A"], ["C"]), self.user)
        view3 = reference_store.effective_view(self.db, 3)
        sigs = {(tuple(r["fd"]["lhs"]), r["fd"]["rhs"]) for r in view3["rules"]}
        self.assertIn((("P",), "R"), sigs)       # 更正后的派生规则
        self.assertNotIn((("P",), "Q"), sigs)    # 旧派生规则已换版
        own = [r for r in view3["rules"] if r["kind"] == "own"]
        self.assertEqual(len(own), 1)            # 末端自有规则原样保留
        self.assertEqual(own[0]["fd"], {"lhs": ["P"], "rhs": "R"})

    def test_failed_propagation_keeps_whole_chain_on_old_version(self):
        reference_store.create_reference(
            self.db, _RefIn(1, 2, {"A": "X", "B": "Y"}), self.user)
        reference_store.create_reference(
            self.db, _RefIn(2, 3, {"X": "P", "Y": "Q"}), self.user)
        dep_svc.create(self.db, _DepIn(1, ["A"], ["B"]), self.user)
        self.db.checkpoint()
        # 注入失败：更正时传播在草案 3 抛错
        engine = reference_store.get_engine(self.db)
        engine.fail_before_relation = 3
        from fastapi import HTTPException as ShimHTTP  # 垫片提供
        dep_id = next(i for i, row in self.db.tables[entities.Dependency].rows.items()
                      if row.relation_id == 1 and row.lhs == ["A"])
        with self.assertRaises(ShimHTTP):
            dep_svc.update(self.db, dep_id, _DepIn(1, ["A"], ["C"]), self.user)
        engine.fail_before_relation = None
        # 整链旧版仍可读到旧规则
        view3 = reference_store.effective_view(self.db, 3)
        self.assertIn({"lhs": ["P"], "rhs": "Q"}, [r["fd"] for r in view3["rules"]])
        # 源草案的本次更正也被一并回滚：仍是 A->B，而非 A->C
        view1 = reference_store.effective_view(self.db, 1)
        source_sigs = {(tuple(r["fd"]["lhs"]), r["fd"]["rhs"]) for r in view1["rules"]}
        self.assertIn((("A",), "B"), source_sigs)
        self.assertNotIn((("A",), "C"), source_sigs)

    def test_empty_determinant_flows_through_service(self):
        reference_store.create_reference(
            self.db, _RefIn(1, 2, {"A": "X"}), self.user)
        dep_svc.create(self.db, _DepIn(1, [], ["A"], note="常量依赖"), self.user)
        view2 = reference_store.effective_view(self.db, 2)
        empty = [r for r in view2["rules"] if r["fd"]["lhs"] == []]
        self.assertEqual(len(empty), 1)
        self.assertEqual(empty[0]["fd"]["rhs"], "X")


if __name__ == "__main__":
    unittest.main(verbosity=2)
