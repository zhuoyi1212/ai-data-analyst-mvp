"""文件型会话存储：所有阶段产物以可检视文件落盘（FR-12）。"""
from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from app.config import settings
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

    # ---- AnalysisBundle（视图级隔离，不与单 plan 槽位冲突） ----
    def bundle_dir(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "bundle"

    def write_bundle(self, session_id: str, payload: Any) -> Path:
        d = self.bundle_dir(session_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "bundle.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
        return path

    def read_bundle(self, session_id: str) -> Any:
        path = self.bundle_dir(session_id) / "bundle.json"
        if not path.exists():
            raise StorageError("尚未生成分析蓝图（AnalysisBundle）。")
        return json.loads(path.read_text(encoding="utf-8"))

    def has_bundle(self, session_id: str) -> bool:
        return (self.bundle_dir(session_id) / "bundle.json").exists()

    def write_bundle_execution(self, session_id: str, payload: Any) -> Path:
        d = self.bundle_dir(session_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "execution.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
        return path

    def read_bundle_execution(self, session_id: str) -> Any:
        path = self.bundle_dir(session_id) / "execution.json"
        if not path.exists():
            raise StorageError("分析蓝图尚未执行。")
        return json.loads(path.read_text(encoding="utf-8"))

    def has_bundle_execution(self, session_id: str) -> bool:
        return (self.bundle_dir(session_id) / "execution.json").exists()

    def view_dir(self, session_id: str, view_id: str) -> Path:
        # view_id 来自受控的规则生成（view_ 前缀），仍做路径安全防护
        safe = Path(view_id).name
        if safe != view_id or not safe:
            raise StorageError(f"非法的视图标识：{view_id}")
        d = self.bundle_dir(session_id) / "views" / safe
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

    def write_view_validation(self, session_id: str, view_id: str, payload: Any) -> Path:
        path = self.view_dir(session_id, view_id) / "validation.json"
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
