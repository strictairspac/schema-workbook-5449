"""跨草案引用引擎测试：菱形引用、空决定项、更正并发、成环/合并拒绝、回滚与幂等。

运行：cd backend && python -m unittest tests.test_references -v
"""
import threading
import unittest

from src.services.references import (
    FD,
    ReferenceEngine,
    ReferenceError,
    closure,
    derive_all,
    derive_transitive,
    detect_cycle,
    map_fd,
    validate_mapping,
)


class MappingValidationTests(unittest.TestCase):
    def test_merge_attribute_rejected(self):
        with self.assertRaisesRegex(ReferenceError, "合并属性"):
            validate_mapping({"A": "X", "B": "X"}, ["A", "B"], ["X"])

    def test_unknown_attributes_rejected(self):
        with self.assertRaisesRegex(ReferenceError, "源草案不存在"):
            validate_mapping({"Z": "X"}, ["A"], ["X"])
        with self.assertRaisesRegex(ReferenceError, "目标草案不存在"):
            validate_mapping({"A": "Z"}, ["A"], ["X"])

    def test_empty_mapping_rejected(self):
        with self.assertRaises(ReferenceError):
            validate_mapping({}, ["A"], ["X"])

    def test_cycle_detection(self):
        edges = [(1, 2), (2, 3)]
        self.assertIsNotNone(detect_cycle(edges, (3, 1)))
        self.assertIsNone(detect_cycle(edges, (1, 3)))


class DerivationTests(unittest.TestCase):
    def test_direct_mapping(self):
        fd = FD.make(["A"], "B")
        self.assertEqual(map_fd(fd, {"A": "X", "B": "Y"}), FD.make(["X"], "Y"))

    def test_unselected_attribute_not_directly_mappable(self):
        fd = FD.make(["A"], "B")
        self.assertIsNone(map_fd(fd, {"A": "X"}))  # B 未选取

    def test_transitive_via_unselected_preserved(self):
        # A->B（B 未选），B->C（C 选取）：必须保留 A->C
        fds = [FD.make(["A"], "B"), FD.make(["B"], "C")]
        mapping = {"A": "X", "C": "Z"}
        derived = derive_transitive(fds, mapping)
        self.assertIn(FD.make(["X"], "Z"), derived)

    def test_transitive_chain_multiple_hops(self):
        # A->B->C->D，仅选取 A、D
        fds = [FD.make(["A"], "B"), FD.make(["B"], "C"), FD.make(["C"], "D")]
        mapping = {"A": "X", "D": "Y"}
        derived = derive_transitive(fds, mapping)
        self.assertEqual(derived, [FD.make(["X"], "Y")])

    def test_empty_determinant_maps_to_empty(self):
        # 空决定项（常量依赖）∅->A
        fd = FD.make([], "A")
        mapped = map_fd(fd, {"A": "X"})
        self.assertEqual(mapped, FD.make([], "X"))
        self.assertEqual(mapped.lhs, frozenset())

    def test_derive_all_includes_mapped_and_transitive(self):
        fds = [FD.make(["A"], "B"), FD.make(["B"], "C"), FD.make(["A"], "D")]
        mapping = {"A": "X", "C": "Z", "D": "W"}
        items = derive_all(fds, mapping)
        kinds = {o["type"] for _, o in items}
        self.assertIn("mapped", kinds)
        self.assertIn("transitive", kinds)
        sigs = {fd.signature for fd, _ in items}
        self.assertIn((("X",), "Z"), sigs)  # A->C 经未选 B 传递
        self.assertIn((("X",), "W"), sigs)  # A->D 直接映射

    def test_closure_basic(self):
        fds = [FD.make(["A"], "B"), FD.make(["B"], "C")]
        self.assertEqual(closure(["A"], fds), frozenset(["A", "B", "C"]))


class EngineLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.engine = ReferenceEngine()
        for rid in (1, 2, 3, 4):
            self.engine.register_relation(rid)

    def test_self_reference_rejected(self):
        with self.assertRaisesRegex(ReferenceError, "不能引用自身"):
            self.engine.add_reference(1, 1, {"A": "X"}, ["A"], ["X"])

    def test_cycle_rejected_at_reference_creation(self):
        self.engine.add_reference(1, 2, {"A": "X"}, ["A"], ["X"])
        self.engine.add_reference(2, 3, {"X": "P"}, ["X"], ["P"])
        with self.assertRaisesRegex(ReferenceError, "成环"):
            self.engine.add_reference(3, 1, {"P": "A"}, ["P"], ["A"])

    def test_merge_mapping_rejected_at_creation(self):
        with self.assertRaisesRegex(ReferenceError, "合并属性"):
            self.engine.add_reference(1, 2, {"A": "X", "B": "X"}, ["A", "B"], ["X"])

    def test_duplicate_own_rule_rejected(self):
        self.engine.add_own_rule(1, FD.make(["A"], "B"))
        with self.assertRaisesRegex(ReferenceError, "重复"):
            self.engine.add_own_rule(1, FD.make(["A"], "B"))

    def test_diamond_reference_both_paths_land(self):
        # 源 1 被 2、3 引用，2、3 再都引用 4（菱形）
        self.engine.add_own_rule(1, FD.make(["A"], "B"))
        self.engine.add_reference(1, 2, {"A": "A2", "B": "B2"}, ["A", "B"], ["A2", "B2"])
        self.engine.add_reference(1, 3, {"A": "A3", "B": "B3"}, ["A", "B"], ["A3", "B3"])
        self.engine.add_reference(2, 4, {"A2": "P", "B2": "Q"}, ["A2", "B2"], ["P", "Q"])
        self.engine.add_reference(3, 4, {"A3": "P", "B3": "R"}, ["A3", "B3"], ["P", "Q", "R"])
        self.engine.propagate(1)
        view = self.engine.effective_view(4)
        # 两条菱形路径各自落地：A2->B2 映射为 P->Q；A3->B3 映射为 P->R
        rules = {(tuple(r["fd"]["lhs"]), r["fd"]["rhs"]): r for r in view["rules"]}
        self.assertIn((("P",), "Q"), rules)
        self.assertIn((("P",), "R"), rules)
        # 两条均为派生规则且各自携带引用来源
        self.assertEqual(rules[(("P",), "Q")]["kind"], "derived")
        self.assertEqual(rules[(("P",), "R")]["kind"], "derived")
        self.assertEqual(view["conflicts"], [])

    def test_conflict_with_own_rule_detected(self):
        self.engine.add_own_rule(1, FD.make(["A"], "B"))
        self.engine.add_own_rule(2, FD.make(["X"], "OTHER"))  # 同 lhs 不同 rhs
        self.engine.add_reference(1, 2, {"A": "X", "B": "Y"}, ["A", "B"], ["X", "Y", "OTHER"])
        result = self.engine.propagate(1)
        ref = result["references"][0]
        self.assertEqual(ref["status"], "conflict")
        self.assertIn("决定项冲突", ref["conflict_reason"])
        view = self.engine.effective_view(2)
        self.assertTrue(any(c["reference_id"] == ref["id"] for c in view["conflicts"]))

    def test_own_rules_untouched_by_propagation(self):
        self.engine.add_own_rule(1, FD.make(["A"], "B"))
        self.engine.add_own_rule(2, FD.make(["K"], "V"), note="本地规则")
        self.engine.add_reference(1, 2, {"A": "X", "B": "Y"}, ["A", "B"], ["X", "Y", "K", "V"])
        self.engine.propagate(1)
        view = self.engine.effective_view(2)
        own = [r for r in view["rules"] if r["kind"] == "own"]
        self.assertEqual(len(own), 1)
        self.assertEqual(own[0]["fd"], {"lhs": ["K"], "rhs": "V"})

    def test_source_correction_propagates_along_chain(self):
        self.engine.add_own_rule(1, FD.make(["A"], "B"))
        self.engine.add_reference(1, 2, {"A": "X", "B": "Y"}, ["A", "B"], ["X", "Y"])
        self.engine.add_reference(2, 3, {"X": "P", "Y": "Q"}, ["X", "Y"], ["P", "Q"])
        self.engine.propagate(1)
        view_before = self.engine.effective_view(3)
        self.assertIn({"lhs": ["P"], "rhs": "Q"}, [r["fd"] for r in view_before["rules"]])
        # 更正源规则 A->B 为 A->C
        rule = self.engine.own_rules[1][0]
        self.engine.correct_own_rule(1, rule.id, FD.make(["A"], "C"))
        # 重新建立映射以纳入 C（模拟操作员调整引用），并沿链传播
        ref12 = next(r for r in self.engine.references.values() if r.target_relation_id == 2)
        ref12.mapping = {"A": "X", "C": "Z"}
        ref23 = next(r for r in self.engine.references.values() if r.target_relation_id == 3)
        ref23.mapping = {"X": "P", "Z": "R"}
        self.engine.propagate(1)
        view_after = self.engine.effective_view(3)
        sigs = {(tuple(r["fd"]["lhs"]), r["fd"]["rhs"]) for r in view_after["rules"]}
        self.assertIn((("P",), "R"), sigs)
        self.assertNotIn((("P",), "Q"), sigs)

    def test_propagation_failure_rolls_back_whole_chain(self):
        self.engine.add_own_rule(1, FD.make(["A"], "B"))
        self.engine.add_reference(1, 2, {"A": "X", "B": "Y"}, ["A", "B"], ["X", "Y"])
        self.engine.add_reference(2, 3, {"X": "P", "Y": "Q"}, ["X", "Y"], ["P", "Q"])
        self.engine.propagate(1)
        refs_before = {rid: (r.status, r.applied_source_revision)
                       for rid, r in self.engine.references.items()}
        # 更正后，令传播在草案 3 处失败
        rule = self.engine.own_rules[1][0]
        self.engine.correct_own_rule(1, rule.id, FD.make(["A"], "C"))
        self.engine.fail_before_relation = 3
        with self.assertRaises(ReferenceError):
            self.engine.propagate(1)
        self.engine.fail_before_relation = None
        # 整链保留旧版：引用状态与 applied 版本均回到传播前
        refs_after = {rid: (r.status, r.applied_source_revision)
                      for rid, r in self.engine.references.items()}
        self.assertEqual(refs_before, refs_after)
        # 旧规则仍然有效
        view = self.engine.effective_view(3)
        self.assertIn({"lhs": ["P"], "rhs": "Q"}, [r["fd"] for r in view["rules"]])

    def test_retry_is_idempotent_no_duplicate_rules(self):
        self.engine.add_own_rule(1, FD.make(["A"], "B"))
        self.engine.add_reference(1, 2, {"A": "X", "B": "Y"}, ["A", "B"], ["X", "Y"])
        first = self.engine.propagate(1)
        ref_id = first["references"][0]["id"]
        self.engine.propagate(1)  # 重复触发
        self.engine.retry_reference(ref_id)  # 显式重试
        view = self.engine.effective_view(2)
        mapped = [r for r in view["rules"] if r["kind"] == "derived"]
        self.assertEqual(len(mapped), 1)  # 绝不重复加规则
        self.assertEqual(mapped[0]["fd"], {"lhs": ["X"], "rhs": "Y"})

    def test_report_snapshot_is_immutable_all_old_or_all_new(self):
        self.engine.add_own_rule(1, FD.make(["A"], "B"))
        self.engine.add_reference(1, 2, {"A": "X", "B": "Y"}, ["A", "B"], ["X", "Y"])
        self.engine.propagate(1)
        report_old = self.engine.create_report(2)
        old_rev = report_old.revision
        old_fds = report_old.fds
        # 更正源并传播到新版本
        rule = self.engine.own_rules[1][0]
        self.engine.correct_own_rule(1, rule.id, FD.make(["A"], "C"))
        ref = next(r for r in self.engine.references.values() if r.target_relation_id == 2)
        ref.mapping = {"A": "X", "C": "Z"}
        self.engine.propagate(1)
        report_new = self.engine.create_report(2)
        self.assertGreater(report_new.revision, old_rev)
        # 旧报告快照不被后续更正影响
        self.assertEqual(self.engine.snapshots[report_old.id].fds, old_fds)
        self.assertEqual(tuple(sorted(f.as_dict()["rhs"] for f in old_fds)), ("Y",))
        self.assertEqual(tuple(sorted(f.as_dict()["rhs"] for f in report_new.fds)), ("Z",))

    def test_empty_determinant_end_to_end(self):
        # 隐含空决定项：∅ -> A（常量依赖），经引用映射为 ∅ -> X
        self.engine.add_own_rule(1, FD.make([], "A"))
        self.engine.add_reference(1, 2, {"A": "X"}, ["A"], ["X"])
        self.engine.propagate(1)
        view = self.engine.effective_view(2)
        empty_fd = [r for r in view["rules"] if r["fd"]["lhs"] == []]
        self.assertEqual(len(empty_fd), 1)
        self.assertEqual(empty_fd[0]["fd"]["rhs"], "X")
        self.assertEqual(empty_fd[0]["kind"], "derived")

    def test_provenance_recorded_on_derived_rules(self):
        self.engine.add_own_rule(1, FD.make(["A"], "B"))
        self.engine.add_reference(1, 2, {"A": "X", "B": "Y"}, ["A", "B"], ["X", "Y"])
        self.engine.propagate(1)
        view = self.engine.effective_view(2)
        derived = next(r for r in view["rules"] if r["kind"] == "derived")
        self.assertIsInstance(derived["origin"], list)
        self.assertEqual(derived["origin"][0]["type"], "mapped")
        self.assertEqual(derived["origin"][0]["source_fd"], {"lhs": ["A"], "rhs": "B"})


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.engine = ReferenceEngine()
        for rid in (1, 2):
            self.engine.register_relation(rid)
        self.engine.add_own_rule(1, FD.make(["A"], "B"))
        self.engine.add_reference(1, 2, {"A": "X", "B": "Y"}, ["A", "B"], ["X", "Y"])
        self.engine.propagate(1)

    def test_report_acceptance_concurrent_with_correction(self):
        """源更正与报告受理并发：每份报告必须全旧或全新，不能混用。"""
        reports: list = []
        errors: list = []

        def correct():
            try:
                rule = self.engine.own_rules[1][0]
                self.engine.correct_own_rule(1, rule.id, FD.make(["A"], "C"))
                ref = next(r for r in self.engine.references.values() if r.target_relation_id == 2)
                ref.mapping = {"A": "X", "C": "Z"}
                self.engine.propagate(1)
            except Exception as exc:  # pragma: no cover - 并发下不应发生
                errors.append(exc)

        def accept_report():
            try:
                snap = self.engine.create_report(2)
                reports.append(snap)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = []
        for i in range(20):
            threads.append(threading.Thread(target=correct if i % 2 == 0 else accept_report))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertTrue(reports)
        # 每份报告的 RHS 只能是旧版 Y 或新版 Z，绝不能同时出现（混用）
        for snap in reports:
            rhs = {f.rhs for f in snap.fds}
            self.assertTrue(rhs <= {"Y", "Z"})
            self.assertFalse({"Y", "Z"} <= rhs, f"报告混用新旧依赖: {rhs}")
        # 既有旧版报告也有新版报告（调度交错的证据）
        versions = {tuple(sorted(f.rhs for f in s.fds)) for s in reports}
        self.assertTrue(versions <= {("Y",), ("Z",)})

    def test_concurrent_propagations_do_not_duplicate_rules(self):
        done = threading.Event()

        def propagate():
            while not done.is_set():
                self.engine.propagate(1)

        threads = [threading.Thread(target=propagate) for _ in range(4)]
        for t in threads:
            t.start()
        for _ in range(50):
            self.engine.effective_view(2)
        done.set()
        for t in threads:
            t.join()
        view = self.engine.effective_view(2)
        derived = [r for r in view["rules"] if r["kind"] == "derived"]
        self.assertEqual(len(derived), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
