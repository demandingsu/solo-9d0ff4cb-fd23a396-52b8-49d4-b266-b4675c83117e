from __future__ import annotations

import io
import os
import re

import pytest

from inspection import create_app
from inspection.db import connect


@pytest.fixture()
def app(tmp_path):
    application = create_app(
        {
            "DATABASE_PATH": str(tmp_path / "inspection.db"),
            "TESTING": True,
            "CSRF_ENABLED": False,
            "SECRET_KEY": "test-secret",
        }
    )
    yield application


@pytest.fixture()
def client(app):
    return app.test_client()


def upload(client, csv_text: str, *, road="G101", day="2026-11-24"):
    return client.post(
        "/upload",
        data={
            "road": road,
            "date": day,
            "file": (io.BytesIO(csv_text.encode("utf-8")), "records.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def test_index_and_health(client):
    assert client.get("/").status_code == 200
    assert client.get("/healthz").json == {"status": "ok"}


def test_upload_then_view_and_download(client):
    response = upload(
        client,
        "记录号,整米里程,病害等级\n"
        "A-1,100,轻\n"
        "A-2,250,重\n",
    )
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "上传成功" in body
    assert "新增 2 条" in body
    assert "A-1" not in body  # the page shows counts, not the raw rows

    page = client.get("/?road=G101&date=2026-11-24")
    assert "已收记录 <strong>2</strong> 条" in page.get_data(as_text=True)

    download = client.get("/download?road=G101&date=2026-11-24&start=0")
    assert download.status_code == 200
    assert download.mimetype == "text/csv"
    assert "attachment" in download.headers["Content-Disposition"]
    text = download.data.decode("utf-8-sig")
    assert text.splitlines() == [
        "记录号,整米里程,病害等级",
        "A-1,100,轻",
        "A-2,250,重",
    ]


def test_identical_reupload_is_accepted_once(client):
    csv_text = "记录号,整米里程,病害等级\nA-1,100,轻\n"
    upload(client, csv_text)
    page = upload(client, csv_text).get_data(as_text=True)
    assert "原样补报 1 条未重复写入" in page
    assert "已收记录 <strong>1</strong> 条" in client.get(
        "/?road=G101&date=2026-11-24"
    ).get_data(as_text=True)


def test_conflicting_reupload_is_rejected(client):
    upload(client, "记录号,整米里程,病害等级\nA-1,100,轻\n")
    page = upload(
        client, "记录号,整米里程,病害等级\nA-1,100,重\n"
    ).get_data(as_text=True)
    assert "存在内容冲突" in page
    assert "A-1" in page
    # Original content is untouched.
    download = client.get("/download?road=G101&date=2026-11-24&start=0")
    assert download.data.decode("utf-8-sig").splitlines()[1] == "A-1,100,轻"


@pytest.mark.parametrize("bad", ["1_000", "+100", "-5", "100.0"])
def test_malformed_mileage_forms_are_rejected(client, bad):
    page = upload(
        client, f"记录号,整米里程,病害等级\nA,{bad},轻\n"
    ).get_data(as_text=True)
    assert "整米里程必须是非负整数" in page


def test_malformed_csv_reports_line_and_writes_nothing(client):
    page = upload(
        client, "记录号,整米里程,病害等级\nA-1,100,轻\nA-2,abc,重\n"
    ).get_data(as_text=True)
    assert "第 3 行" in page
    assert "已收记录 <strong>0</strong> 条" in client.get(
        "/?road=G101&date=2026-11-24"
    ).get_data(as_text=True)


def test_split_via_web_and_cut_goes_right(client):
    upload(client, "记录号,整米里程,病害等级\nA,100,轻\nB,150,重\nC,200,轻\n")
    page = client.post(
        "/split",
        data={"road": "G101", "date": "2026-11-24", "cut": "150"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "拆分完成" in page
    assert "共搬移 3 条" in page
    assert "[0, 150)" in page
    assert "[150, +∞)" in page

    left = client.get(
        "/download?road=G101&date=2026-11-24&start=0&end=149"
    ).data.decode("utf-8-sig").splitlines()
    right = client.get(
        "/download?road=G101&date=2026-11-24&start=150"
    ).data.decode("utf-8-sig").splitlines()
    assert left[1:] == ["A,100,轻"]
    assert right[1:] == ["B,150,重", "C,200,轻"]


def test_invalid_split_does_not_change_layout(client, app):
    upload(client, "记录号,整米里程,病害等级\nA,100,轻\n")
    page = client.post(
        "/split",
        data={"road": "G101", "date": "2026-11-24", "cut": "0"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "找不到包含该切点的未拆分分区" in page
    with connect(app.config["DATABASE_PATH"]) as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM partitions WHERE status='active'"
        ).fetchone()[0]
        rows = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    assert (active, rows) == (1, 1)


def test_download_rejects_inverted_range(client):
    upload(client, "记录号,整米里程,病害等级\nA,100,轻\n")
    page = client.get(
        "/download?road=G101&date=2026-11-24&start=200&end=100",
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "结束里程不能小于起始里程" in page


def test_csrf_is_required_for_posts(tmp_path):
    application = create_app(
        {
            "DATABASE_PATH": str(tmp_path / "csrf.db"),
            "TESTING": True,
            "SECRET_KEY": "csrf-secret",
        }
    )
    client = application.test_client()
    page = client.get("/?road=G101&date=2026-11-24").get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)

    rejected = client.post(
        "/upload",
        data={"road": "G101", "date": "2026-11-24"},
        content_type="multipart/form-data",
    )
    assert rejected.status_code == 400

    accepted = client.post(
        "/split",
        data={
            "road": "G101",
            "date": "2026-11-24",
            "cut": "10",
            "csrf_token": token,
        },
        follow_redirects=True,
    )
    # No records/partition exist yet, so this is a validation message but the
    # request passed CSRF.
    assert "找不到包含该切点的未拆分分区" in accepted.get_data(as_text=True)


def test_download_name_is_sanitized(client):
    upload(client, "记录号,整米里程,病害等级\nA,100,轻\n", road="G101 主线")
    response = client.get(
        "/download?road=G101%20%E4%B8%BB%E7%BA%BF&date=2026-11-24&start=0"
    )
    disposition = response.headers["Content-Disposition"]
    assert "attachment" in disposition
    assert ".csv" in disposition


def test_upload_rejects_path_like_road(client):
    page = upload(
        client,
        "记录号,整米里程,病害等级\nA,100,轻\n",
        road="../etc",
    ).get_data(as_text=True)
    assert "公路名称不能包含" in page


def test_upload_size_limit(tmp_path):
    application = create_app(
        {
            "DATABASE_PATH": str(tmp_path / "limit.db"),
            "TESTING": True,
            "CSRF_ENABLED": False,
            "MAX_CONTENT_LENGTH": 200,
        }
    )
    client = application.test_client()
    big = "记录号,整米里程,病害等级\n" + "A,100,轻\n" * 100
    response = client.post(
        "/upload",
        data={
            "road": "G101",
            "date": "2026-11-24",
            "file": (io.BytesIO(big.encode()), "big.csv"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 413
