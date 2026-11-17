"""跨草案引用引擎（纯函数内核）。

设计目标（对应需求条款）：
* 选取源草案属性并映射为目标草案属性（attribute mapping）。
* 源规则按映射推导为目标规则；经过未选属性传递得到的依赖仍然保留。
* 映射出现合并属性（多个源属性 -> 同一目标属性）或引用成环 -> 拒绝。
* 源规则更正后沿引用链传播更新；目标自有规则原样保留。
* 引用结果处于 pending / effective / conflict 三态，并附带推导来源。
* 传播事务化：任一环节失败，整链保留旧版本；重试幂等，不重复产生规则。
* 报告只在一个已提交版本上取快照：全旧或全新，不混用（快照不可变）。

本模块不接触数据库与网络，所有状态都装在 :class:`ReferenceEngine` 里，便于精确单测。
派生规则按引用“物化”为已提交版本：传播成功才整体换版，失败由备份恢复，
因此更正途中受理的报告只能读到全旧或全新的有效依赖。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from threading import RLock
from typing import Any, Iterable


class ReferenceError(ValueError):
    """引用建立/传播被拒绝（硬错误，调用方应整体回滚）。"""


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FD:
    """单 RHS 函数依赖；lhs 为 frozenset，允许空集（常量依赖）。"""

    lhs: frozenset[str]
    rhs: str

    @staticmethod
    def make(lhs: Iterable[str], rhs: str) -> "FD":
        return FD(frozenset(lhs), rhs)

    def as_dict(self) -> dict[str, Any]:
        return {"lhs": sorted(self.lhs), "rhs": self.rhs}

    @property
    def signature(self) -> tuple:
        """规则指纹：同 lhs/rhs 即同一条规则，用于幂等去重。"""
        return (tuple(sorted(self.lhs)), self.rhs)


@dataclass
class Reference:
    id: int
    source_relation_id: int
    target_relation_id: int
    # 源属性 -> 目标属性（只登记被选取的属性；未选属性不进映射）
    mapping: dict[str, str]
    status: str = "pending"  # pending | effective | conflict
    conflict_reason: str | None = None
    # 最近一次成功传播所依据的源版本，失败时保持旧值
    applied_source_revision: int | None = None


@dataclass
class Rule:
    """规则登记簿中的一条记录。own=自有，derived=引用推导。"""

    id: int
    relation_id: int
    fd: FD
    kind: str  # own | derived
    origin: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReportSnapshot:
    id: int
    relation_id: int
    revision: int  # 冻结时的全局版本
    fds: tuple[FD, ...]
    origins: tuple[dict[str, Any], ...]
    conflicts: tuple[dict[str, Any], ...] = ()


# --------------------------------------------------------------------------- #
# 纯函数工具
# --------------------------------------------------------------------------- #
def closure(attributes: Iterable[str], fds: Iterable[FD]) -> frozenset[str]:
    out = set(attributes)
    while True:
        size = len(out)
        for fd in fds:
            if fd.lhs <= out:
                out.add(fd.rhs)
        if len(out) == size:
            return frozenset(out)


def validate_mapping(mapping: dict[str, str], source_attrs: list[str], target_attrs: list[str]) -> None:
    if not mapping:
        raise ReferenceError("至少选取一个源属性进行映射")
    if any(not src.strip() or not dst.strip() for src, dst in mapping.items()):
        raise ReferenceError("映射属性不能为空")
    # 合并属性：多个源属性映射到同一个目标属性
    targets = list(mapping.values())
    if len(set(targets)) != len(targets):
        dup = sorted({t for t in targets if targets.count(t) > 1})
        raise ReferenceError(f"映射出现合并属性，目标属性 {dup} 被多个源属性映射，拒绝建立引用")
    unknown_src = set(mapping) - set(source_attrs)
    if unknown_src:
        raise ReferenceError(f"源草案不存在属性 {sorted(unknown_src)}")
    unknown_dst = set(targets) - set(target_attrs)
    if unknown_dst:
        raise ReferenceError(f"目标草案不存在属性 {sorted(unknown_dst)}")


def map_fd(fd: FD, mapping: dict[str, str]) -> FD | None:
    """把一条源 FD 按映射翻译到目标草案。

    依赖 FD 中出现的每个属性都必须被选取；未选属性上的传递依赖由
    :func:`derive_transitive` 单独保留，这里只处理完全在选取集内的规则。
    返回 None 表示该规则直接引用了未选属性，不能直译。
    """
    used = fd.lhs | {fd.rhs}
    if not used <= set(mapping):
        return None
    return FD.make({mapping[a] for a in fd.lhs}, mapping[fd.rhs])


def derive_transitive(source_fds: list[FD], mapping: dict[str, str]) -> list[FD]:
    """保留经未选属性传递得到的依赖。

    思路：在源草案上计算属性闭包。对选取属性的任意子集 X，
    若全量闭包含有另一个选取属性 y，而仅由选取属性构成的 FD 无法推出 y
    （即推出路径必须经过未选属性），则输出映射后的 X→y。
    这样 A→B（B 未选）、B→C（C 选取）会保留为 A→C。
    """
    selected = set(mapping)
    direct: set[FD] = set()
    for fd in source_fds:
        mapped = map_fd(fd, mapping)
        if mapped is not None:
            direct.add(mapped)
    selected_only_fds = [fd for fd in source_fds if (fd.lhs | {fd.rhs}) <= selected]
    out: set[FD] = set()
    selected_sorted = sorted(selected)
    for size in range(len(selected_sorted) + 1):
        for subset in combinations(selected_sorted, size):
            x = frozenset(subset)
            full_closure = closure(x, source_fds) & selected
            short_closure = closure(x, selected_only_fds) & selected
            for y in full_closure - short_closure - x:
                out.add(FD.make({mapping[a] for a in x}, mapping[y]))
    return sorted(out - direct, key=lambda f: f.signature)


def derive_all(source_fds: list[FD], mapping: dict[str, str]) -> list[tuple[FD, dict[str, Any]]]:
    """生成目标侧全部派生规则及其推导来源。"""
    result: list[tuple[FD, dict[str, Any]]] = []
    for fd in source_fds:
        mapped = map_fd(fd, mapping)
        if mapped is None:
            continue
        result.append((mapped, {
            "type": "mapped",
            "source_fd": fd.as_dict(),
            "via": dict(mapping),
        }))
    for fd in derive_transitive(source_fds, mapping):
        result.append((fd, {
            "type": "transitive",
            "via_unselected": True,
            "via": dict(mapping),
        }))
    return result


def detect_cycle(edges: list[tuple[int, int]], new_edge: tuple[int, int]) -> list[int] | None:
    """若加入 new_edge（source->target）后成环，返回环上的节点 id，否则 None。"""
    graph: dict[int, set[int]] = {}
    for src, dst in edges + [new_edge]:
        graph.setdefault(src, set()).add(dst)
    stack: list[tuple[int, list[int]]] = [(new_edge[1], [new_edge[1]])]
    seen: set[int] = set()
    while stack:
        node, path = stack.pop()
        for nxt in graph.get(node, ()):
            if nxt == new_edge[0]:
                return path + [nxt]
            if nxt not in seen:
                seen.add(nxt)
                stack.append((nxt, path + [nxt]))
    return None


# --------------------------------------------------------------------------- #
# 有状态引擎
# --------------------------------------------------------------------------- #
class ReferenceEngine:
    """线程安全的内存引用内核；持久化层把它的决策落库。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self.revisions: dict[int, int] = {}          # relation_id -> 自有规则版本
        self.own_rules: dict[int, list[Rule]] = {}
        self.references: dict[int, Reference] = {}
        # 已提交的派生规则：relation_id -> reference_id -> [(FD, origin), ...]
        # 传播成功时整体换版；失败经备份恢复，因此旧版始终可读。
        self.committed_derived: dict[int, dict[int, list[tuple[FD, dict[str, Any]]]]] = {}
        self._ref_seq = 0
        self._rule_seq = 0
        self._report_seq = 0
        self.global_revision = 0
        # 失败注入钩子：传播到指定 relation 前抛错，用于整链回滚测试
        self.fail_before_relation: int | None = None
        # 已完成的传播意图（引用 id, 源版本）-> 成功，保证重试不重复加规则
        self.applied_intents: set[tuple[int, int]] = set()
        self.snapshots: dict[int, ReportSnapshot] = {}

    # ----- 基础登记 ------------------------------------------------------- #
    def register_relation(self, relation_id: int) -> None:
        with self._lock:
            self.revisions.setdefault(relation_id, 0)
            self.own_rules.setdefault(relation_id, [])
            self.committed_derived.setdefault(relation_id, {})

    def _bump(self, relation_id: int) -> None:
        self.revisions[relation_id] = self.revisions.get(relation_id, 0) + 1
        self.global_revision += 1

    def own_fds(self, relation_id: int) -> list[FD]:
        return [r.fd for r in self.own_rules.get(relation_id, [])]

    def add_own_rule(self, relation_id: int, fd: FD, note: str = "") -> Rule:
        with self._lock:
            self.register_relation(relation_id)
            if any(r.fd.signature == fd.signature for r in self.own_rules[relation_id]):
                raise ReferenceError("同一草案内已存在重复的相同函数依赖")
            self._rule_seq += 1
            rule = Rule(self._rule_seq, relation_id, fd, "own", {"type": "own", "note": note})
            self.own_rules[relation_id].append(rule)
            self._bump(relation_id)
            return rule

    def correct_own_rule(self, relation_id: int, rule_id: int, new_fd: FD) -> Rule:
        """源规则更正：替换 FD 并提升版本，随后由 propagate 沿链更新。"""
        with self._lock:
            rules = self.own_rules.get(relation_id, [])
            for rule in rules:
                if rule.id == rule_id:
                    if any(r.id != rule_id and r.fd.signature == new_fd.signature for r in rules):
                        raise ReferenceError("更正后与同草案另一条规则重复")
                    rule.fd = new_fd
                    self._bump(relation_id)
                    return rule
            raise ReferenceError("自有规则不存在")

    # ----- 引用建立 ------------------------------------------------------- #
    def add_reference(
        self,
        source_relation_id: int,
        target_relation_id: int,
        mapping: dict[str, str],
        source_attrs: list[str],
        target_attrs: list[str],
    ) -> Reference:
        with self._lock:
            if source_relation_id == target_relation_id:
                raise ReferenceError("草案不能引用自身")
            validate_mapping(mapping, source_attrs, target_attrs)
            edges = [(r.source_relation_id, r.target_relation_id) for r in self.references.values()]
            cycle = detect_cycle(edges, (source_relation_id, target_relation_id))
            if cycle:
                raise ReferenceError(f"引用成环（{' -> '.join(map(str, cycle))}），拒绝建立")
            self._ref_seq += 1
            ref = Reference(
                id=self._ref_seq,
                source_relation_id=source_relation_id,
                target_relation_id=target_relation_id,
                mapping=dict(mapping),
                status="pending",
            )
            self.references[ref.id] = ref
            self.committed_derived.setdefault(target_relation_id, {})
            return ref

    def update_mapping(self, ref_id: int, mapping: dict[str, str],
                       source_attrs: list[str], target_attrs: list[str]) -> Reference:
        """调整引用映射（如源更正后纳入新属性）；换映射后需重新传播。"""
        with self._lock:
            ref = self.references.get(ref_id)
            if not ref:
                raise ReferenceError("引用不存在")
            validate_mapping(mapping, source_attrs, target_attrs)
            ref.mapping = dict(mapping)
            ref.status = "pending"
            ref.applied_source_revision = None
            return ref

    def outgoing(self, relation_id: int) -> list[Reference]:
        return [r for r in self.references.values() if r.source_relation_id == relation_id]

    def incoming(self, relation_id: int) -> list[Reference]:
        return [r for r in self.references.values() if r.target_relation_id == relation_id]

    # ----- 有效视图 ------------------------------------------------------- #
    def _effective_fds(self, relation_id: int) -> list[FD]:
        fds = list(self.own_fds(relation_id))
        for entries in self.committed_derived.get(relation_id, {}).values():
            fds.extend(fd for fd, _ in entries)
        # 同指纹去重（自有优先）
        unique: dict[tuple, FD] = {}
        for fd in fds:
            unique.setdefault(fd.signature, fd)
        return list(unique.values())

    def effective_view(self, relation_id: int) -> dict[str, Any]:
        """页面/报告消费的有效依赖视图：自有 + 已提交派生，含冲突说明与来源。"""
        with self._lock:
            own = [
                {"fd": r.fd.as_dict(), "kind": "own", "origin": r.origin}
                for r in self.own_rules.get(relation_id, [])
            ]
            derived: list[dict[str, Any]] = []
            conflicts: list[dict[str, Any]] = []
            for ref in self.incoming(relation_id):
                if ref.status == "conflict":
                    conflicts.append({
                        "reference_id": ref.id,
                        "source_relation_id": ref.source_relation_id,
                        "status": "conflict",
                        "reason": ref.conflict_reason,
                    })
                    continue
                if ref.status != "effective":
                    conflicts.append({
                        "reference_id": ref.id,
                        "source_relation_id": ref.source_relation_id,
                        "status": "pending",
                        "reason": "引用待检查，尚未生效",
                    })
                    continue
                for fd, origin in self.committed_derived.get(relation_id, {}).get(ref.id, []):
                    derived.append({
                        "fd": fd.as_dict(),
                        "kind": "derived",
                        "origin": {**origin, "reference_id": ref.id,
                                   "source_relation_id": ref.source_relation_id},
                    })
            merged: dict[tuple, dict[str, Any]] = {}
            for item in own + derived:
                sig = (tuple(item["fd"]["lhs"]), item["fd"]["rhs"])
                if sig in merged:
                    merged[sig].setdefault("origins", []).append(item["origin"])
                else:
                    merged[sig] = {"fd": item["fd"], "kind": item["kind"],
                                   "origins": [item["origin"]]}
            return {
                "relation_id": relation_id,
                "revision": self.revisions.get(relation_id, 0),
                "rules": [
                    {"fd": v["fd"], "kind": v["kind"],
                     "origin": v["origins"][0] if v["kind"] == "own" else v["origins"]}
                    for v in sorted(merged.values(), key=lambda x: (x["fd"]["lhs"], x["fd"]["rhs"]))
                ],
                "conflicts": conflicts,
            }

    # ----- 传播 ----------------------------------------------------------- #
    def _topo_targets(self, start_relation: int) -> list[int]:
        """从被更正草案出发沿引用边拓扑排序，近源目标排在前面。"""
        order: list[int] = []
        visited: set[int] = set()

        def walk(node: int) -> None:
            for ref in self.outgoing(node):
                if ref.target_relation_id not in visited:
                    visited.add(ref.target_relation_id)
                    walk(ref.target_relation_id)
                    order.append(ref.target_relation_id)

        walk(start_relation)
        order.reverse()
        return order

    def _recompute_reference(self, ref: Reference) -> list[tuple[FD, dict[str, Any]]]:
        """基于源草案当前已提交有效依赖，重算该引用的派生规则并链接上游来源。"""
        source_fds = self._effective_fds(ref.source_relation_id)
        upstream = [r.id for r in self.incoming(ref.source_relation_id) if r.status == "effective"]
        entries = []
        for fd, origin in derive_all(source_fds, ref.mapping):
            origin = {**origin, "upstream_reference_ids": upstream}
            entries.append((fd, origin))
        return entries

    def _check_conflict(self, ref: Reference, entries: list[tuple[FD, dict[str, Any]]]) -> str | None:
        """冲突：派生规则与目标自有规则决定项相同但被决定项不同。"""
        own_by_lhs: dict[frozenset[str], set[str]] = {}
        for fd in self.own_fds(ref.target_relation_id):
            own_by_lhs.setdefault(fd.lhs, set()).add(fd.rhs)
        for fd, _ in entries:
            if fd.lhs in own_by_lhs and fd.rhs not in own_by_lhs[fd.lhs]:
                return (
                    f"派生规则 {sorted(fd.lhs)}→{fd.rhs} 与目标自有规则 "
                    f"{sorted(fd.lhs)}→{sorted(own_by_lhs[fd.lhs])} 决定项冲突"
                )
        return None

    def propagate(self, start_relation: int) -> dict[str, Any]:
        """源更正后沿引用链更新，整链事务：任一环节失败则全部保留旧版。

        幂等：已应用的 (引用, 源版本) 意图直接跳过，重试绝不重复加规则。
        """
        with self._lock:
            backup = self._snapshot_state()
            try:
                targets = self._topo_targets(start_relation)
                touched: list[Reference] = []
                for target in targets:
                    if self.fail_before_relation == target:
                        raise ReferenceError(f"模拟传播失败：草案 {target} 不可用")
                    for ref in self.incoming(target):
                        source_rev = self.revisions.get(ref.source_relation_id, 0)
                        if ref.status == "effective" and ref.applied_source_revision == source_rev:
                            continue  # 幂等：该版本已成功传播
                        if (ref.id, source_rev) in self.applied_intents:
                            continue
                        entries = self._recompute_reference(ref)
                        conflict = self._check_conflict(ref, entries)
                        if conflict:
                            ref.status = "conflict"
                            ref.conflict_reason = conflict
                            self.committed_derived[target].pop(ref.id, None)
                        else:
                            ref.status = "effective"
                            ref.conflict_reason = None
                            ref.applied_source_revision = source_rev
                            self.applied_intents.add((ref.id, source_rev))
                            # 整体换版：旧条目被同一引用的新条目原子替换
                            self.committed_derived[target][ref.id] = entries
                        # 目标有效内容发生变化（派生换版或冲突摘除），提升版本，
                        # 使下游引用在本次拓扑传播中能观察到新源版本。
                        self._bump(target)
                        touched.append(ref)
                self.global_revision += 1
                return {
                    "start_relation": start_relation,
                    "targets": targets,
                    "references": [self._ref_dict(r) for r in touched],
                    "global_revision": self.global_revision,
                }
            except Exception:
                self._restore_state(backup)
                raise

    def retry_reference(self, ref_id: int) -> dict[str, Any]:
        """冲突/失败后重试：重新走传播，幂等不重复加规则。"""
        with self._lock:
            ref = self.references.get(ref_id)
            if not ref:
                raise ReferenceError("引用不存在")
            return self.propagate(ref.source_relation_id)

    # ----- 报告快照（全旧或全新，绝不混用） ------------------------------- #
    def take_report_snapshot(self, report_id: int, relation_id: int) -> ReportSnapshot:
        with self._lock:
            view = self.effective_view(relation_id)
            snap = ReportSnapshot(
                id=report_id,
                relation_id=relation_id,
                revision=self.global_revision,
                fds=tuple(FD.make(item["fd"]["lhs"], item["fd"]["rhs"]) for item in view["rules"]),
                origins=tuple({"fd": item["fd"], "kind": item["kind"], "origin": item["origin"]}
                              for item in view["rules"]),
                conflicts=tuple(view["conflicts"]),
            )
            self._report_seq = max(self._report_seq, report_id)
            self.snapshots[report_id] = snap
            return snap

    def create_report(self, relation_id: int) -> ReportSnapshot:
        with self._lock:
            self._report_seq += 1
            return self.take_report_snapshot(self._report_seq, relation_id)

    # ----- 状态备份/恢复（整链回滚） -------------------------------------- #
    def _snapshot_state(self) -> dict[str, Any]:
        import copy
        return {
            "revisions": copy.deepcopy(self.revisions),
            "own_rules": copy.deepcopy(self.own_rules),
            "references": copy.deepcopy(self.references),
            "committed_derived": copy.deepcopy(self.committed_derived),
            "global_revision": self.global_revision,
            "applied_intents": set(self.applied_intents),
            "rule_seq": self._rule_seq,
            "ref_seq": self._ref_seq,
            "report_seq": self._report_seq,
        }

    def _restore_state(self, backup: dict[str, Any]) -> None:
        import copy
        self.revisions = copy.deepcopy(backup["revisions"])
        self.own_rules = copy.deepcopy(backup["own_rules"])
        self.references = copy.deepcopy(backup["references"])
        self.committed_derived = copy.deepcopy(backup["committed_derived"])
        self.global_revision = backup["global_revision"]
        self.applied_intents = set(backup["applied_intents"])
        self._rule_seq = backup["rule_seq"]
        self._ref_seq = backup["ref_seq"]
        self._report_seq = backup["report_seq"]

    def _ref_dict(self, ref: Reference) -> dict[str, Any]:
        return {
            "id": ref.id,
            "source_relation_id": ref.source_relation_id,
            "target_relation_id": ref.target_relation_id,
            "mapping": dict(ref.mapping),
            "status": ref.status,
            "conflict_reason": ref.conflict_reason,
            "applied_source_revision": ref.applied_source_revision,
        }

    def reference_listing(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._ref_dict(r) for r in self.references.values()]
