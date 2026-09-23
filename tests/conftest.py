"""pytest 公共夹具：每个用例使用独立临时 SQLite 文件。"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setenv("EVIDENCE_DB", tmp.name)

    # 延迟导入，确保 EVIDENCE_DB 已生效；startup 时自动建库建表
    from app.main import app

    with TestClient(app) as c:
        yield c

    app.state.db.close()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(tmp.name + suffix)
        except OSError:
            pass
