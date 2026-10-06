"""T12 评测案例清单（机器可读真值）。

每个 EvalCase 描述一份输入数据与其期望行为（真值），供
scripts/run_evaluation.py 批量消费。数据构造器**直接复用**现有 T07/T08/T09
测试中的确定性 builder（固定种子、字节稳定），避免评测数据与测试数据漂移：

- tests/test_t09_diagnostic_search.py：known_issue / no_problem / 高基数干扰 / 预算压力
- tests/test_t08_cross_metric_signals.py：结构反转（Simpson）/ 增收不增利

案例类型对应《Trae执行清单》T12 要求的六类带真值诊断案例：
known_issue（已知问题）、no_problem（无问题）、structural_reversal（结构反转）、
partial_period（残缺周期）、spurious_correlation（异常点假相关）、
insufficient_data（数据不足）；stress 为预算压力案例，不计入定位正确率。

后续补齐第 2 项缺口时（异常点假相关 / 真实小样本 / 残缺周期诊断案例），
只需在本清单注册，harness 无需改动。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from itertools import product
from pathlib import Path
from typing import Callable, Literal

import pandas as pd

# 测试目录下的 builder 是真值数据的唯一权威来源；评测脚本复用而非复制。
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_TESTS_DIR = _BACKEND_ROOT / "tests"
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

import test_t08_cross_metric_signals as _t08  # noqa: E402
import test_t09_diagnostic_search as _t09  # noqa: E402

CaseType = Literal[
    "known_issue",          # 已知问题：存在预埋的真实业务问题
    "no_problem",           # 无问题：不应产出告警/下钻
    "structural_reversal",  # 结构反转：各组改善但整体恶化
    "partial_period",       # 残缺周期（待补）
    "spurious_correlation", # 异常点假相关（待补）
    "insufficient_data",    # 数据不足（待补）
    "stress",               # 预算压力：多维度均有信号，只考核预算硬边界
]

Domain = Literal["retail", "marketing", "operations", "synthetic"]

# 真值的判定方式：
# - branch：受控 Loop 必须把下钻分支锚定 expected_dimension + expected_member
#   （静态 Bundle 无下钻树，降级为文本命中 expected_member）
# - finding_keyword：Dashboard Finding 标题必须命中任一 expected_keywords
# - relationship_absence：不得产出引用 relationship 视图的 Finding，
#   且不得存在可消费 relationship 视图（假相关/小样本门禁）
TruthKind = Literal["branch", "finding_keyword", "relationship_absence"]

# 阴性案例的误报口径：
# - any_finding：任何 Finding（及 Loop 下钻分支）都算误报（真无问题数据）
# - relationship_only：只把「相关关系结论」算误报；数据中真实存在的异常点告警合法
FalsePositivePolicy = Literal["any_finding", "relationship_only"]

QualityMode = Literal["suggested", "safe"]  # safe=仅转格式，保留离群/缺失


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    title: str
    case_type: CaseType
    domain: Domain
    builder: Callable[[], bytes]
    expect_signal: bool                     # 该数据是否存在应被报告的真实信号
    truth_kind: TruthKind | None = None
    expected_dimension: str | None = None   # branch 真值：问题维度
    expected_member: str | None = None      # branch 真值：问题成员
    expected_keywords: tuple[str, ...] = ()  # finding_keyword 真值：标题关键词
    semantic_overrides: dict[str, str] = field(default_factory=dict)
    fp_policy: FalsePositivePolicy = "any_finding"
    quality_mode: QualityMode = "suggested"
    expected_stop_reasons: tuple[str, ...] = ()  # Loop 允许的停止原因（空=不考核）
    note: str = ""


# ================================================================ T12 第 2 批 builder


def _spurious_correlation_csv() -> bytes:
    """异常点假相关：29 个与 X 正交的 Y 点（总体 r=0）+ 1 个 (120,120) 极端点。

    全样本 Pearson r≈0.92，但剔除该离群点后 r=0：典型「单点制造的相关」。
    该点在 X/Y 单变量上均为离群值，因此质量处理必须走 safe（保留）路径，
    模拟用户未确认剔除离群点的真实场景。
    """
    n_base = 29
    x = list(range(n_base)) + [120]
    y = [1.0 if i % 2 == 0 else -1.0 for i in range(n_base)] + [120.0]
    dates = pd.date_range("2024-01-01", periods=n_base + 1, freq="D")
    df = pd.DataFrame({
        "Date": dates.strftime("%Y-%m-%d"),
        "Region": ["A" if i % 2 else "B" for i in range(n_base + 1)],
        "X": [float(v) for v in x],
        "Y": y,
    })
    return df.to_csv(index=False).encode("utf-8")


def _sparse_pairs_csv() -> bytes:
    """真实小样本：40 行快照，但 X/Y 仅在 6 行同时非空且近乎完全相关。

    planner 的 30 行预筛挡不住该场景（快照有 40 行），相关算子删失成对后
    n=6 仍能算出 r≈1；必须由执行器稳定性门禁显式拒绝。
    缺失值走 safe（保留）路径，不允许均值插补制造虚假样本对。
    """
    rows = []
    for i in range(40):
        month, day = (1, i + 1) if i < 30 else (2, i - 29)
        rows.append({
            "Date": f"2024-{month:02d}-{day:02d}",
            "Region": "A" if i % 2 else "B",
            "X": float(i + 1) if i < 6 else None,
            "Y": float(2 * (i + 1)) if i < 6 else None,
        })
    return pd.DataFrame(rows).to_csv(index=False).encode("utf-8")


def _partial_period_csv() -> bytes:
    """残缺周期：1 月 31 天完整、2 月仅有 1–10 日；每单元每天恒定 100。

    朴素总量口径下 2 月只有 1 月的 10/31，看似暴跌 68%；等长 MTD 口径
    （2/1–2/10 对比 1/1–1/10）逐成员完全一致，必须零信号、零告警。
    """
    dims = {
        "Region": ["North", "South", "East", "West"],
        "Segment": ["Consumer", "Corporate"],
        "Channel": ["Online", "Retail"],
    }
    dates = [f"2024-01-{d:02d}" for d in range(1, 32)]
    dates += [f"2024-02-{d:02d}" for d in range(1, 11)]
    rows = []
    for ds in dates:
        for combo in product(*dims.values()):
            rows.append({
                "Date": ds, **dict(zip(dims.keys(), combo)), "Sales": 100.0,
            })
    return pd.DataFrame(rows).to_csv(index=False).encode("utf-8")


CASES: tuple[EvalCase, ...] = (
    EvalCase(
        case_id="known_issue_region_collapse",
        title="区域腰斩：North 2 月销售额 100→40，贡献全部下滑",
        case_type="known_issue",
        domain="retail",
        builder=_t09._golden_csv,
        expect_signal=True,
        truth_kind="branch",
        expected_dimension="Region",
        expected_member="North",
        note="T09 黄金案例：contribution_share≈1，恒等式残差为 0。",
    ),
    EvalCase(
        case_id="no_problem_uniform",
        title="两期完全一致、组间均匀：无任何信号",
        case_type="no_problem",
        domain="synthetic",
        builder=_t09._uniform_csv,
        expect_signal=False,
        note="首轮 3 probes 后必须 no_signal 提前停，0 分支、0 Finding。",
    ),
    EvalCase(
        case_id="known_issue_high_cardinality_distractor",
        title="真实问题在 Region，高基数 Customer 维度为干扰项",
        case_type="known_issue",
        domain="retail",
        builder=_t09._high_card_csv,
        expect_signal=True,
        truth_kind="branch",
        expected_dimension="Region",
        expected_member="North",
        semantic_overrides={"Customer": "dimension"},
        note="Customer 100 成员、每实体 1–2 行：应被基数惩罚/support 拦截，"
             "正确分支仍锚定 Region=North。",
    ),
    EvalCase(
        case_id="structural_reversal_simpson",
        title="Simpson 悖论：各组利润率改善，结构倾斜致整体率下降 13pp",
        case_type="structural_reversal",
        domain="retail",
        builder=_t08._simpson_csv_bytes,
        expect_signal=True,
        truth_kind="finding_keyword",
        expected_keywords=("结构悖论",),
        note="T08 黄金案例②：Finding 必须引用结构权重（mix）效应。",
    ),
    EvalCase(
        case_id="known_issue_scale_profit_divergence",
        title="增收不增利：Sales +20%，Profit −70%",
        case_type="known_issue",
        domain="retail",
        builder=_t08._divergence_csv_bytes,
        expect_signal=True,
        truth_kind="finding_keyword",
        expected_keywords=("增收不增利",),
        note="T08 黄金案例①：跨指标背离 + 会计拆解（非因果）证据。",
    ),
    EvalCase(
        case_id="stress_six_signal_dimensions",
        title="预算压力：6 个维度各有弱势成员",
        case_type="stress",
        domain="synthetic",
        builder=_t09._budget_csv,
        expect_signal=True,
        note="不设单一真值、不计入定位正确率；只考核 probe≤8 / 执行≤24 / "
             "深度≤3 等预算硬边界与耗时。",
    ),
    EvalCase(
        case_id="spurious_correlation_single_outlier",
        title="异常点假相关：29 个正交点 + 1 个极端点，全样本 r≈0.92/留一 r=0",
        case_type="spurious_correlation",
        domain="synthetic",
        builder=_spurious_correlation_csv,
        expect_signal=False,
        truth_kind="relationship_absence",
        fp_policy="relationship_only",
        quality_mode="safe",
        note="离群点真实存在，anomaly 告警合法；禁止产出任何相关关系结论。"
             "门禁：relationship_stability=fail，散点保留在视图字典。",
    ),
    EvalCase(
        case_id="insufficient_data_sparse_pairs",
        title="数据不足：40 行中仅 6 对 X/Y 同时非空，r≈1 但样本不足",
        case_type="insufficient_data",
        domain="synthetic",
        builder=_sparse_pairs_csv,
        expect_signal=False,
        truth_kind="relationship_absence",
        fp_policy="relationship_only",
        quality_mode="safe",
        note="planner 30 行预筛无法拦截（删失后才暴露 n=6）；执行器必须显式"
             "拒绝并中文说明，而非产出「强相关」。",
    ),
    EvalCase(
        case_id="partial_period_mtd_no_signal",
        title="残缺周期：2 月仅 10 天，等长 MTD 口径下零变化（朴素口径看似 −68%）",
        case_type="partial_period",
        domain="retail",
        builder=_partial_period_csv,
        expect_signal=False,
        expected_stop_reasons=("no_signal",),
        note="T05 等长窗口的诊断层验收：Loop 必须 no_signal 干净停止、0 分支；"
             "静态 Dashboard 零 Finding。",
    ),
)

CASES_BY_ID: dict[str, EvalCase] = {c.case_id: c for c in CASES}
