"""Flask web application for road inspection records."""

from __future__ import annotations

import io
import logging
import os
import re
import secrets
import sqlite3
from datetime import date as date_type

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from .csvio import CsvFormatError, parse_records
from .db import connect, init_db
from .service import (
    ConflictError,
    ServiceError,
    ValidationError,
    day_overview,
    download_csv,
    list_roads,
    range_label,
    split_day,
    upload_records,
)

LOGGER = logging.getLogger("inspection.web")

CONTROL_OR_PATH = re.compile(r"[\x00-\x1f\x7f/\\]")


def _default_db_path() -> str:
    return os.environ.get(
        "DATABASE_PATH",
        os.path.join(os.getcwd(), "instance", "inspection.db"),
    )


def _valid_road(raw: str) -> str:
    road = (raw or "").strip()
    if not road:
        raise ValidationError("请填写公路名称")
    if len(road) > 100:
        raise ValidationError("公路名称不能超过 100 个字符")
    if CONTROL_OR_PATH.search(road):
        raise ValidationError("公路名称不能包含控制字符或路径分隔符")
    return road


def _valid_date(raw: str) -> str:
    value = (raw or "").strip()
    try:
        return date_type.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValidationError("日期格式应为 YYYY-MM-DD") from exc


def _parse_int(raw: str, label: str, *, allow_empty: bool = False) -> int | None:
    value = (raw or "").strip()
    if not value:
        if allow_empty:
            return None
        raise ValidationError(f"请填写{label}")
    try:
        number = int(value, 10)
    except ValueError as exc:
        raise ValidationError(f"{label}必须是整数") from exc
    if number < 0:
        raise ValidationError(f"{label}不能为负数")
    if number > 9_223_372_036_854_775_807:
        raise ValidationError(f"{label}超出可存储范围")
    return number


def _download_name(road: str, day: str, start: int, end: int | None) -> str:
    safe_road = CONTROL_OR_PATH.sub("_", road)
    safe_road = re.sub(r'[\s:*?"<>|]+', "_", safe_road).strip("._") or "inspection"
    tail = f"{start}-{end}" if end is not None else f"{start}-end"
    return f"{safe_road}_{day}_{tail}.csv"


def create_app(test_config: dict | None = None) -> Flask:
    package_dir = os.path.dirname(os.path.abspath(__file__))
    app = Flask(
        __name__,
        template_folder=os.path.join(package_dir, "..", "templates"),
        static_folder=os.path.join(package_dir, "..", "static"),
    )
    secret_key = os.environ.get("SECRET_KEY")
    if not secret_key:
        if os.environ.get("REQUIRE_SECRET_KEY") == "1":
            raise RuntimeError("SECRET_KEY must be set")
        secret_key = secrets.token_hex(32)
        LOGGER.warning(
            "SECRET_KEY not set; using a random per-process key. Sessions "
            "(and CSRF tokens) will not survive a restart."
        )
    app.config.from_mapping(
        DATABASE_PATH=_default_db_path(),
        SECRET_KEY=secret_key,
        MAX_CONTENT_LENGTH=int(os.environ.get("MAX_UPLOAD_BYTES", 32 * 1024 * 1024)),
        SPLIT_BATCH_SIZE=int(os.environ.get("SPLIT_BATCH_SIZE", 500)),
        CSRF_ENABLED=True,
    )
    if test_config:
        app.config.update(test_config)

    if not app.config["SECRET_KEY"]:
        raise RuntimeError("SECRET_KEY must not be empty")

    init_db(app.config["DATABASE_PATH"])

    @app.template_filter("range_label")
    def _range_label(lo: int, hi: int | None) -> str:
        return range_label(lo, hi)

    def csrf_token() -> str:
        token = session.get("_csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["_csrf_token"] = token
        return token

    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.before_request
    def _check_csrf():
        if request.method != "POST" or not app.config["CSRF_ENABLED"]:
            return None
        expected = session.get("_csrf_token", "")
        supplied = request.form.get("csrf_token", "")
        if not expected or not supplied or not secrets.compare_digest(
            expected, supplied
        ):
            abort(400, description="表单已过期，请刷新页面后重试")
        return None

    @app.get("/healthz")
    def healthz():
        try:
            with connect(app.config["DATABASE_PATH"]) as conn:
                conn.execute("SELECT 1").fetchone()
        except Exception:
            LOGGER.exception("health check failed")
            return {"status": "error"}, 503
        return {"status": "ok"}

    @app.get("/")
    def index():
        roads = []
        overview = None
        road = request.args.get("road", "").strip()
        day = request.args.get("date", "").strip() or date_type.today().isoformat()
        error = None
        try:
            with connect(app.config["DATABASE_PATH"]) as conn:
                roads = list_roads(conn)
                if road:
                    road = _valid_road(road)
                    day = _valid_date(day)
                    overview = day_overview(conn, road, day)
                else:
                    day = _valid_date(day)
        except ServiceError as exc:
            error = str(exc)
        return render_template(
            "index.html",
            roads=roads,
            overview=overview,
            selected_road=road,
            selected_date=day,
            error=error,
        )

    def _back_to_day(status: int = 303):
        return redirect(
            url_for(
                "index",
                road=request.form.get("road") or request.args.get("road"),
                date=request.form.get("date") or request.args.get("date"),
            ),
            code=status,
        )

    @app.post("/upload")
    def upload():
        try:
            road = _valid_road(request.form.get("road", ""))
            day = _valid_date(request.form.get("date", ""))
            upload = request.files.get("file")
            if upload is None or not upload.filename:
                raise ValidationError("请选择要上传的 CSV 文件")
            content = upload.read()
            if not content:
                raise ValidationError("上传的 CSV 文件为空")
            records = parse_records(content)
            with connect(app.config["DATABASE_PATH"]) as conn:
                result = upload_records(conn, road, day, records)
        except CsvFormatError as exc:
            for message in exc.messages:
                flash(message, "error")
            return _back_to_day()
        except ConflictError as exc:
            flash(str(exc), "error")
            for message in exc.conflicts[:20]:
                flash(message, "error")
            return _back_to_day()
        except ServiceError as exc:
            flash(str(exc), "error")
            return _back_to_day()
        except sqlite3.Error:
            LOGGER.exception("upload failed due to database error")
            flash("写入数据库失败，本次上传未生效，请重试", "error")
            return _back_to_day()

        flash(
            f"上传成功：共 {result.total_rows} 行，新增 {result.inserted} 条，"
            f"原样补报 {result.unchanged} 条未重复写入；"
            f"当天现有 {result.partition_count} 个有效分区。",
            "success",
        )
        return redirect(url_for("index", road=road, date=day), code=303)

    @app.post("/split")
    def split():
        road = request.form.get("road", "")
        day = request.form.get("date", "")
        try:
            road = _valid_road(road)
            day = _valid_date(day)
            cut = _parse_int(request.form.get("cut", ""), "切点里程")
            moved = split_day(
                app.config["DATABASE_PATH"],
                road,
                day,
                int(cut),
                batch_size=app.config["SPLIT_BATCH_SIZE"],
            )
        except (ServiceError, CsvFormatError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("index", road=road, date=day), code=303)
        except sqlite3.Error:
            LOGGER.exception("split failed due to database error")
            flash("拆分过程中数据库出错，原分区未切换，可重试", "error")
            return redirect(url_for("index", road=road, date=day), code=303)
        except Exception:
            LOGGER.exception("split failed unexpectedly")
            flash("拆分过程中发生未预期错误，原分区未被切换，可重试", "error")
            return redirect(url_for("index", road=road, date=day), code=303)

        flash(
            f"拆分完成：切点 {cut} 归右侧，共搬移 {moved} 条记录；"
            "两份新分区均已生效。",
            "success",
        )
        return redirect(url_for("index", road=road, date=day), code=303)

    @app.get("/download")
    def download():
        try:
            road = _valid_road(request.args.get("road", ""))
            day = _valid_date(request.args.get("date", ""))
            start = _parse_int(request.args.get("start", "0"), "起始里程")
            end = _parse_int(
                request.args.get("end", ""), "结束里程", allow_empty=True
            )
            if end is not None and end < int(start):
                raise ValidationError("结束里程不能小于起始里程")
            with connect(app.config["DATABASE_PATH"]) as conn:
                data = download_csv(conn, road, day, int(start), end)
        except ServiceError as exc:
            flash(str(exc), "error")
            return redirect(
                url_for(
                    "index",
                    road=request.args.get("road"),
                    date=request.args.get("date"),
                ),
                code=303,
            )
        return send_file(
            io.BytesIO(data),
            mimetype="text/csv; charset=utf-8",
            as_attachment=True,
            download_name=_download_name(road, day, int(start), end),
        )

    @app.errorhandler(400)
    def bad_request(exc):
        message = getattr(exc, "description", "请求无效")
        return (
            render_template(
                "error.html",
                title="请求无效",
                message=message,
            ),
            400,
        )

    @app.errorhandler(404)
    def not_found(exc):
        return (
            render_template("error.html", title="页面不存在", message="请检查链接"),
            404,
        )

    @app.errorhandler(413)
    def too_large(exc):
        return (
            render_template(
                "error.html",
                title="文件过大",
                message="上传文件超过大小限制，请拆分后重试",
            ),
            413,
        )

    return app
