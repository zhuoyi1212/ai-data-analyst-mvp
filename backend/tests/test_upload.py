"""TR-3.1 / TR-3.2：上传边界解析、会话落盘与 API。"""
from __future__ import annotations

import io

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.parser import UploadError, normalize_mixed_columns, parse_table
from app.services.storage import SessionStore


def _csv_bytes(n: int = 20, encoding: str = "utf-8") -> bytes:
    df = pd.DataFrame(
        {
            "订单号": [f"A{i:04d}" for i in range(n)],
            "金额": [10.5 + i for i in range(n)],
            "区域": ["华北"] * n,
        }
    )
    return df.to_csv(index=False).encode(encoding)


def _xlsx_bytes(n: int = 20) -> bytes:
    df = pd.DataFrame({"id": range(n), "name": [f"p{i}" for i in range(n)], "v": [1.0] * n})
    bio = io.BytesIO()
    df.to_excel(bio, index=False, engine="openpyxl")
    return bio.getvalue()


# ---------- 解析边界（TR-3.1） ----------

def test_parse_csv_utf8():
    t = parse_table("sales.csv", _csv_bytes())
    assert t.rows == 20 and t.cols == 3
    assert t.physical_types["金额"] == "float"
    assert len(t.preview()) == 10


def test_parse_csv_gbk():
    t = parse_table("sales_gbk.csv", _csv_bytes(encoding="gbk"))
    assert "订单号" in t.df.columns


def test_parse_xlsx():
    t = parse_table("book.xlsx", _xlsx_bytes())
    assert t.rows == 20 and t.cols == 3


def test_reject_bad_suffix():
    with pytest.raises(UploadError, match="文件类型"):
        parse_table("note.txt", b"hello")


def test_reject_empty_and_header_only():
    with pytest.raises(UploadError, match="文件为空"):
        parse_table("empty.csv", b"")
    with pytest.raises(UploadError, match="数据行"):
        parse_table("header.csv", "a,b,c\n".encode("utf-8"))


def test_reject_size_rows_cols_limits():
    with pytest.raises(UploadError, match="过大"):
        parse_table("big.csv", _csv_bytes(5), max_file_mb=0)
    with pytest.raises(UploadError, match="行数"):
        parse_table("rows.csv", _csv_bytes(11), max_rows=10)
    df = pd.DataFrame({f"c{i}": [1] for i in range(6)})
    with pytest.raises(UploadError, match="列数"):
        parse_table("cols.csv", df.to_csv(index=False).encode(), max_cols=5)


def test_reject_duplicate_and_empty_headers():
    with pytest.raises(UploadError, match="重复列名"):
        parse_table("dup.csv", "a,a,b\n1,2,3\n".encode("utf-8"))
    with pytest.raises(UploadError, match="空表头"):
        parse_table("blank.csv", "a,,b\n1,2,3\n".encode("utf-8"))


# ---------- 会话落盘（TR-3.2） ----------

def test_session_persisted_and_reloadable(tmp_path):
    store = SessionStore(tmp_path / "storage")
    meta = store.create_from_bytes("sales.csv", _csv_bytes(15))
    sid = meta["session_id"]
    assert meta["shape"] == {"rows": 15, "cols": 3}

    d = tmp_path / "storage" / sid
    assert (d / "original.csv").exists()
    assert (d / "meta.json").exists()
    assert (d / "preview.json").exists()

    reloaded = SessionStore(tmp_path / "storage").load_original(sid)
    assert reloaded.shape == (15, 3)


def test_upload_api_and_error(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "storage")
    monkeypatch.setattr("app.routers.sessions._store", lambda: store)
    client = TestClient(app)

    resp = client.post(
        "/sessions/upload",
        files={"file": ("sales.csv", _csv_bytes(12), "text/csv")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["meta"]["shape"]["rows"] == 12
    assert len(body["preview"]["rows"]) == 10

    bad = client.post(
        "/sessions/upload",
        files={"file": ("note.txt", b"x", "text/plain")},
    )
    assert bad.status_code == 400 and "文件类型" in bad.json()["detail"]


# ---------- 混合类型列归一化（Excel 数字+文本混合列，如 Postal Code） ----------

def test_normalize_mixed_postal_code_column_can_write_parquet(tmp_path):
    """int+str 混合 object 列（美国数字邮编 + 加拿大 M7A 文本）归一化后可写 Parquet。"""
    df = pd.DataFrame(
        {
            "Postal Code": [90001, 90002, "M7A", 10001],
            "City": ["Los Angeles", "Los Angeles", "Toronto", "New York"],
            "Sales": [12.5, 33.0, 44.2, 10.0],
        }
    )
    out = normalize_mixed_columns(df)
    # 混合邮编列按文本处理，数字邮编转字符串，M7A 保留
    assert str(out["Postal Code"].dtype) == "string"
    assert out["Postal Code"].tolist() == ["90001", "90002", "M7A", "10001"]
    # 纯文本列与数值列不被改动
    assert str(out["City"].dtype) in {"object", "str"}
    assert str(out["Sales"].dtype) == "float64"
    # 关键：归一化后能成功写 Parquet（修复前 pyarrow 抛 ArrowInvalid）
    out.to_parquet(tmp_path / "snapshot.parquet", index=False)
    back = pd.read_parquet(tmp_path / "snapshot.parquet")
    assert back["Postal Code"].tolist() == ["90001", "90002", "M7A", "10001"]


def test_normalize_pure_numeric_object_column_promotes_to_number():
    """纯数字的 object 列（无文本混入）应提升为数值列，保留计算语义。"""
    df = pd.DataFrame({"code": [1, 2, 3]}, dtype=object)
    out = normalize_mixed_columns(df)
    assert pd.api.types.is_numeric_dtype(out["code"])
    assert out["code"].sum() == 6
