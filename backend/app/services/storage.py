"""文件型会话存储：所有阶段产物以可检视文件落盘（FR-12）。

P0 T01 起，Bundle 链路产物按「运行版本」隔离：
    bundle/
      runs/{run_id}/manifest.json | bundle.json | execution.json | dashboard.json
      runs/{run_id}/views/{view_id}/...
      runs/{run_id}/probes/{probe_id}/...
      active.json   工作流最新运行（规划/执行/发布均更新）
      current.json  最近一次已发布的完整版本（消费端只认该指针）
旧运行目录永久保留可读；current 与 active 不一致时读取方必须按 stale 处理。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from app.config import settings
from app.schemas.run import RunManifest, RunPointer, RunStatus
from app.services.parser import ParsedTable, normalize_mixed_columns, parse_table

# 工作流阶段（顺序即状态机顺序）
STAGES = [
    "upload",
    "profile",
    "quality",
    "questions",
    "plan",
    "execute",
    "validate",
    "visualize",
    "insight",
    "followup",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StorageError(KeyError):
    pass


class StaleRunError(Exception):
    """下游产物与当前运行版本/口径不匹配（HTTP 层映射为 409）。"""


class SessionStore:
    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir or settings.storage_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def session_dir(self, session_id: str) -> Path:
        d = self.base_dir / session_id
        if not d.exists():
            raise StorageError(f"会话不存在：{session_id}")
        return d

    # ---- 创建 / 列举 ----
    def create_from_bytes(self, filename: str, data: bytes) -> dict[str, Any]:
        table = parse_table(filename, data)  # 先校验，失败不落盘
        session_id = uuid.uuid4().hex
        d = self.base_dir / session_id
        d.mkdir(parents=True)
        suffix = Path(filename).suffix.lower()
        original = d / f"original{suffix}"
        original.write_bytes(data)  # 原始字节原样保留
        self._write_meta_and_preview(session_id, filename, original.name, table)
        return self.get_meta(session_id)

    def create_from_sample(self, sample_name: str) -> dict[str, Any]:
        sample_root = settings.sample_data_dir.resolve()
        path = (sample_root / sample_name).resolve()
        if sample_root not in path.parents and path != sample_root:
            raise StorageError("非法的示例数据名称。")
        if not path.exists():
            raise StorageError(f"示例数据不存在：{sample_name}")
        meta = self.create_from_bytes(path.name, path.read_bytes())
        meta["source_sample"] = sample_name
        self.write_artifact(meta["session_id"], "meta", meta)
        return meta

    def _write_meta_and_preview(
        self, session_id: str, filename: str, original_name: str, table: ParsedTable
    ) -> None:
        meta = {
            "session_id": session_id,
            "filename": filename,
            "created_at": _now(),
            "stage": "upload",
            "shape": {"rows": table.rows, "cols": table.cols},
            "physical_types": table.physical_types,
            "original_file": original_name,
        }
        self.write_artifact(session_id, "meta", meta)
        self.write_artifact(
            session_id, "preview", {"columns": list(table.df.columns), "rows": table.preview()}
        )

    def list_sessions(self) -> list[dict[str, Any]]:
        metas = []
        for d in sorted(self.base_dir.iterdir(), key=lambda p: p.name):
            meta_file = d / "meta.json"
            if d.is_dir() and meta_file.exists():
                metas.append(json.loads(meta_file.read_text(encoding="utf-8")))
        return metas

    # ---- 产物读写 ----
    def write_artifact(self, session_id: str, name: str, payload: Any) -> Path:
        d = self.session_dir(session_id)
        path = d / f"{name}.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
        return path

    def read_artifact(self, session_id: str, name: str) -> Any:
        path = self.session_dir(session_id) / f"{name}.json"
        if not path.exists():
            raise StorageError(f"阶段产物不存在：{name}")
        return json.loads(path.read_text(encoding="utf-8"))

    def has_artifact(self, session_id: str, name: str) -> bool:
        return (self.session_dir(session_id) / f"{name}.json").exists()

    def get_meta(self, session_id: str) -> dict[str, Any]:
        return self.read_artifact(session_id, "meta")

    def update_meta(self, session_id: str, **changes: Any) -> dict[str, Any]:
        meta = self.get_meta(session_id)
        meta.update(changes)
        self.write_artifact(session_id, "meta", meta)
        return meta

    def save_snapshot(self, session_id: str, df: pd.DataFrame) -> str:
        """保存处理后数据快照（parquet），返回 sha256。"""
        import hashlib

        # Parquet 要求明确 schema：归一化 object 混合类型列（如数字+文本混合的邮编列）
        df = normalize_mixed_columns(df)
        path = self.session_dir(session_id) / "snapshot.parquet"
        df.to_parquet(path, index=False)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return digest

    def load_snapshot(self, session_id: str) -> pd.DataFrame:
        path = self.session_dir(session_id) / "snapshot.parquet"
        if not path.exists():
            raise StorageError("数据快照尚未生成，请先完成数据质量处理。")
        return pd.read_parquet(path)

    def copy_sample_into(self, src: Path, session_id: str, filename: str) -> None:
        shutil.copy(src, self.session_dir(session_id) / filename)

    # ---- AnalysisBundle：运行版本化存储（P0 T01） ----
    def bundle_dir(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "bundle"

    def _runs_dir(self, session_id: str) -> Path:
        return self.bundle_dir(session_id) / "runs"

    def _pointer_path(self, session_id: str, name: str) -> Path:
        return self.bundle_dir(session_id) / f"{name}.json"

    def _read_pointer(self, session_id: str, name: str) -> dict[str, Any] | None:
        path = self._pointer_path(session_id, name)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_pointer_atomic(
        self, session_id: str, name: str, pointer: RunPointer
    ) -> None:
        """指针发布必须原子替换：迟到响应只能整体成功或整体不可见。"""
        d = self.bundle_dir(session_id)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".{name}.tmp"
        tmp.write_text(
            json.dumps(pointer.model_dump(mode="json"),
                       ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
        os.replace(tmp, self._pointer_path(session_id, name))

    def active_run_id(self, session_id: str) -> str | None:
        p = self._read_pointer(session_id, "active")
        return p["run_id"] if p else None

    def current_run_id(self, session_id: str) -> str | None:
        p = self._read_pointer(session_id, "current")
        return p["run_id"] if p else None

    def is_dashboard_stale(self, session_id: str) -> bool:
        """current（已发布）存在但不等于 active（最新工作流运行）即陈旧。"""
        cur, act = self.current_run_id(session_id), self.active_run_id(session_id)
        return cur is not None and act is not None and cur != act

    def run_dir(self, session_id: str, run_id: str) -> Path:
        safe = Path(run_id).name
        if safe != run_id or not safe:
            raise StorageError(f"非法的运行标识：{run_id}")
        d = self._runs_dir(session_id) / safe
        d.mkdir(parents=True, exist_ok=True)
        return d

    def active_run_dir(self, session_id: str) -> Path:
        run_id = self.active_run_id(session_id)
        if run_id is None:
            raise StorageError("尚未生成分析蓝图（AnalysisBundle）。")
        return self.run_dir(session_id, run_id)

    def current_run_dir(self, session_id: str) -> Path:
        run_id = self.current_run_id(session_id)
        if run_id is None:
            raise StorageError("尚未发布仪表盘（DashboardArtifact）。")
        return self.run_dir(session_id, run_id)

    def run_manifest(
        self, session_id: str, run_id: str | None = None
    ) -> RunManifest:
        rid = run_id or self.active_run_id(session_id)
        if rid is None:
            raise StorageError("尚未生成分析蓝图（AnalysisBundle）。")
        path = self._runs_dir(session_id) / rid / "manifest.json"
        if not path.exists():
            raise StorageError(f"运行版本清单不存在：{rid}")
        return RunManifest.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )

    def _update_manifest(
        self, session_id: str, run_id: str, **changes: Any
    ) -> RunManifest:
        rd = self.run_dir(session_id, run_id)
        manifest = self.run_manifest(session_id, run_id)
        updated = manifest.model_copy(update=changes)
        (rd / "manifest.json").write_text(
            json.dumps(updated.model_dump(mode="json"),
                       ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
        return updated

    @staticmethod
    def _sha256_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _sha256_file(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _canonical_sha(payload: Any) -> str:
        body = json.dumps(
            payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), default=_json_default,
        ).encode("utf-8")
        return hashlib.sha256(body).hexdigest()

    def write_bundle(
        self,
        session_id: str,
        payload: Any,
        *,
        scope_filters: list[dict[str, Any]] | None = None,
    ) -> Path:
        """规划新运行：分配 run_id、固化口径指纹并切换 active 指针。

        旧运行目录原样保留（可读、可审计），但立即不再是工作流当前版本。
        T06：scope_filters（全局筛选）固化进 manifest 并参与 scope_hash；
        空筛选时 scope_hash 等于 snapshot_hash（与 P0 行为一致）。
        """
        session_dir = self.session_dir(session_id)
        quality = self.read_artifact(session_id, "quality")
        snapshot_hash = quality["snapshot_hash"]
        dict_path = session_dir / "dictionary.json"
        dictionary_hash = self._sha256_file(dict_path)
        scope_filters = scope_filters or []
        if scope_filters:
            scope_hash = self._canonical_sha(
                {"snapshot": snapshot_hash, "filters": scope_filters}
            )
        else:
            scope_hash = snapshot_hash  # 无全局筛选，范围即整张快照
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        rd = self.run_dir(session_id, run_id)

        path = rd / "bundle.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
        plan_hash = self._sha256_file(path)
        manifest = RunManifest(
            run_id=run_id,
            bundle_id=payload["bundle_id"],
            status=RunStatus.planned,
            snapshot_hash=snapshot_hash,
            dictionary_hash=dictionary_hash,
            scope_hash=scope_hash,
            plan_hash=plan_hash,
            scope=scope_filters,
            created_at=_now(),
        )
        (rd / "manifest.json").write_text(
            json.dumps(manifest.model_dump(mode="json"),
                       ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
        self._write_pointer_atomic(session_id, "active", RunPointer(
            run_id=run_id, bundle_id=payload["bundle_id"], updated_at=_now(),
        ))
        return path

    def read_bundle(self, session_id: str, run_id: str | None = None) -> Any:
        if run_id is not None:
            path = self.run_dir(session_id, run_id) / "bundle.json"
        else:
            rid = self.active_run_id(session_id)
            if rid is None:
                raise StorageError("尚未生成分析蓝图（AnalysisBundle）。")
            path = self.run_dir(session_id, rid) / "bundle.json"
        if not path.exists():
            raise StorageError("分析蓝图文件缺失，请重新生成 AnalysisBundle。")
        return json.loads(path.read_text(encoding="utf-8"))

    def has_bundle(self, session_id: str) -> bool:
        rid = self.active_run_id(session_id)
        return rid is not None and (
            self._runs_dir(session_id) / rid / "bundle.json"
        ).exists()

    def write_bundle_execution(self, session_id: str, payload: Any) -> Path:
        run_id = self.active_run_id(session_id)
        if run_id is None:
            raise StorageError("尚未生成分析蓝图（AnalysisBundle）。")
        manifest = self.run_manifest(session_id, run_id)
        if payload["bundle_id"] != manifest.bundle_id:
            raise StaleRunError(
                "执行结果与当前运行的 Bundle 不一致，请重新执行当前分析蓝图。"
            )
        path = self.run_dir(session_id, run_id) / "execution.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
        self._update_manifest(session_id, run_id, status=RunStatus.executed)
        return path

    def read_bundle_execution(self, session_id: str) -> Any:
        rid = self.active_run_id(session_id)
        if rid is None:
            raise StorageError("分析蓝图尚未执行。")
        path = self.run_dir(session_id, rid) / "execution.json"
        if not path.exists():
            raise StorageError("分析蓝图尚未执行。")
        return json.loads(path.read_text(encoding="utf-8"))

    def has_bundle_execution(self, session_id: str) -> bool:
        rid = self.active_run_id(session_id)
        return rid is not None and (
            self._runs_dir(session_id) / rid / "execution.json"
        ).exists()

    def view_dir(self, session_id: str, view_id: str) -> Path:
        # view_id 来自受控的规则生成（view_ 前缀），仍做路径安全防护
        safe = Path(view_id).name
        if safe != view_id or not safe:
            raise StorageError(f"非法的视图标识：{view_id}")
        d = self.active_run_dir(session_id) / "views" / safe
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_view_ledger(self, session_id: str, view_id: str, payload: Any) -> Path:
        path = self.view_dir(session_id, view_id) / "ledger.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
        return path

    def save_view_result(self, session_id: str, view_id: str, df: pd.DataFrame) -> None:
        # 复用快照同款混合类型归一化，保证任意引擎结果都能写 Parquet
        normalize_mixed_columns(df).to_parquet(
            self.view_dir(session_id, view_id) / "result.parquet", index=False
        )

    def write_view_chart_spec(self, session_id: str, view_id: str, payload: Any) -> Path:
        path = self.view_dir(session_id, view_id) / "chart_spec.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
        return path

    def write_view_validation(self, session_id: str, view_id: str, payload: Any) -> Path:
        path = self.view_dir(session_id, view_id) / "validation.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
        return path

    # ---- 历史运行内的 View 读取（T06：data_ref 翻页，不触碰 active 指针） ----
    @staticmethod
    def _safe_name(name: str, label: str) -> str:
        safe = Path(name).name
        if safe != name or not safe:
            raise StorageError(f"非法的{label}：{name}")
        return safe

    def run_view_dir(self, session_id: str, run_id: str, view_id: str) -> Path:
        rid = self._safe_name(run_id, "运行标识")
        vid = self._safe_name(view_id, "视图标识")
        d = self.run_dir(session_id, rid) / "views" / vid
        if not d.exists():
            raise StorageError(f"运行 {rid} 中不存在视图：{vid}")
        return d

    def read_run_view_result(
        self, session_id: str, run_id: str, view_id: str
    ) -> pd.DataFrame:
        path = self.run_view_dir(session_id, run_id, view_id) / "result.parquet"
        if not path.exists():
            raise StorageError("该视图结果文件不存在。")
        return pd.read_parquet(path)

    def list_runs(self, session_id: str) -> list[dict[str, Any]]:
        """列举全部历史运行（最新在前），含发布状态与固化筛选口径。"""
        runs_root = self._runs_dir(session_id)
        if not runs_root.exists():
            return []
        out: list[dict[str, Any]] = []
        for d in sorted(runs_root.iterdir(), reverse=True):
            manifest_file = d / "manifest.json"
            if not (d.is_dir() and manifest_file.exists()):
                continue
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            out.append({
                "run_id": manifest["run_id"],
                "bundle_id": manifest["bundle_id"],
                "status": manifest["status"].value
                if hasattr(manifest["status"], "value") else manifest["status"],
                "scope": manifest.get("scope", []),
                "has_dashboard": (d / "dashboard.json").exists(),
                "created_at": manifest["created_at"],
                "published_at": manifest.get("published_at"),
            })
        return out

    # ---- DashboardArtifact：只发布完整版本，指针原子切换 ----
    def write_dashboard(
        self, session_id: str, payload: Any, *, expected_run_id: str | None = None
    ) -> Path:
        run_id = self.active_run_id(session_id)
        if run_id is None:
            raise StorageError("尚未生成分析蓝图（AnalysisBundle）。")
        # 迟到响应防护：合成开始后若 active 已切到更新的运行，本次只落历史文件，
        # 绝不回切 current 指针。
        if expected_run_id is not None and run_id != expected_run_id:
            raise StaleRunError(
                "检测到更新的分析运行，本次合成结果不作为当前仪表盘发布。"
            )
        rd = self.run_dir(session_id, run_id)
        path = rd / "dashboard.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
        self._update_manifest(
            session_id, run_id, status=RunStatus.published, published_at=_now()
        )
        self._write_pointer_atomic(session_id, "current", RunPointer(
            run_id=run_id, bundle_id=payload["bundle_id"], updated_at=_now(),
        ))
        return path

    def read_dashboard(self, session_id: str, run_id: str | None = None) -> Any:
        if run_id is not None:
            path = self.run_dir(session_id, run_id) / "dashboard.json"
        else:
            rid = self.current_run_id(session_id)
            if rid is None:
                raise StorageError("尚未生成仪表盘（DashboardArtifact）。")
            path = self.run_dir(session_id, rid) / "dashboard.json"
        if not path.exists():
            raise StorageError("尚未生成仪表盘（DashboardArtifact）。")
        return json.loads(path.read_text(encoding="utf-8"))

    def has_dashboard(self, session_id: str) -> bool:
        rid = self.current_run_id(session_id)
        return rid is not None and (
            self._runs_dir(session_id) / rid / "dashboard.json"
        ).exists()

    # ---- 洞察探针（产物隔离在当前运行的 runs/{id}/probes/） ----
    def probe_dir(self, session_id: str, probe_id: str) -> Path:
        safe = Path(probe_id).name
        if safe != probe_id or not safe:
            raise StorageError(f"非法的探针标识：{probe_id}")
        d = self.active_run_dir(session_id) / "probes" / safe
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_probe_plan(self, session_id: str, probe_id: str, payload: Any) -> Path:
        path = self.probe_dir(session_id, probe_id) / "plan.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
        return path

    def save_probe_result(
        self, session_id: str, probe_id: str, df: pd.DataFrame
    ) -> None:
        normalize_mixed_columns(df).to_parquet(
            self.probe_dir(session_id, probe_id) / "result.parquet", index=False
        )

    def write_probe_ledger(self, session_id: str, probe_id: str, payload: Any) -> Path:
        path = self.probe_dir(session_id, probe_id) / "ledger.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
        return path

    def load_probe_result(self, session_id: str, probe_id: str) -> pd.DataFrame:
        path = self.probe_dir(session_id, probe_id) / "result.parquet"
        if not path.exists():
            raise StorageError(f"探针结果不存在：{probe_id}")
        return pd.read_parquet(path)

    # ---- Diagnostic Search（T09）：产物隔离 runs/{id}/diagnostic/，
    #      与 dashboard 的 probes/（MAX_PROBES=3）目录与计数双隔离 ----
    def diagnostic_dir(self, session_id: str) -> Path:
        return self.active_run_dir(session_id) / "diagnostic"

    def has_diagnostic(self, session_id: str) -> bool:
        try:
            return (self.diagnostic_dir(session_id) / "search.json").exists()
        except StorageError:
            return False

    def write_diagnostic(self, session_id: str, payload: Any) -> Path:
        """search 状态原子替换；每个 probe 后立即落盘，作为恢复时唯一事实。"""
        d = self.diagnostic_dir(session_id)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / ".search.tmp"
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
        path = d / "search.json"
        os.replace(tmp, path)
        return path

    def read_diagnostic(self, session_id: str) -> Any:
        path = self.diagnostic_dir(session_id) / "search.json"
        if not path.exists():
            raise StorageError("尚未发起诊断搜索（DiagnosticSearch）。")
        return json.loads(path.read_text(encoding="utf-8"))

    def _diagnostic_probe_dir(self, session_id: str, probe_id: str) -> Path:
        safe = Path(probe_id).name
        if safe != probe_id or not safe:
            raise StorageError(f"非法的探针标识：{probe_id}")
        d = self.diagnostic_dir(session_id) / "probes" / safe
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_diagnostic_probe_plan(
        self, session_id: str, probe_id: str, plan_key: str, payload: Any
    ) -> Path:
        path = (
            self._diagnostic_probe_dir(session_id, probe_id)
            / f"plan_{plan_key}.json"
        )
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
        return path

    def save_diagnostic_probe_result(
        self, session_id: str, probe_id: str, plan_key: str,
        df: pd.DataFrame,
    ) -> None:
        normalize_mixed_columns(df).to_parquet(
            self._diagnostic_probe_dir(session_id, probe_id)
            / f"result_{plan_key}.parquet",
            index=False,
        )

    def write_diagnostic_probe_ledger(
        self, session_id: str, probe_id: str, plan_key: str, payload: Any
    ) -> Path:
        path = (
            self._diagnostic_probe_dir(session_id, probe_id)
            / f"ledger_{plan_key}.json"
        )
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
        return path

    def load_original(self, session_id: str) -> pd.DataFrame:
        """重新解析原始上传文件，返回 DataFrame。"""
        meta = self.get_meta(session_id)
        path = self.session_dir(session_id) / meta["original_file"]
        table = parse_table(meta["filename"], path.read_bytes())
        return table.df


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "item"):
        try:
            return obj.item()
        except (ValueError, TypeError):
            pass
    if isinstance(obj, (pd.Timestamp,)):
        return obj.isoformat()
    return str(obj)
