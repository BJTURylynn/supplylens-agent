import asyncio
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from supplylens import __version__
from supplylens.agent import AgentService
from supplylens.cloud import check_cloud, cloud_status
from supplylens.config import ROOT, Settings
from supplylens.db import init_database, load_run, make_engine
from supplylens.imports import MAX_FILE_BYTES, ImportConflict, ImportService, ImportValidationError
from supplylens.repository import Repository
from supplylens.schemas import ChatRequest, ChatResponse, ImportCommitRequest


def prepare_sqlite_dir(url: str):
    if url.startswith("sqlite:///") and not url.endswith(":memory:"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)


def create_app(settings: Settings | None = None, *, index=None, model_factory=None):
    config = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.boot_id = uuid4().hex
        app.state.startup_error = None
        app.state.engine = None
        app.state.service = None
        try:
            prepare_sqlite_dir(config.database_url)
            engine = make_engine(config.database_url)
            app.state.engine = engine
            await asyncio.to_thread(init_database, engine, config.auto_seed)
            app.state.service = AgentService(engine, config, index=index, model_factory=model_factory)
            app.state.import_service = ImportService(engine)
        except Exception as exc:
            app.state.startup_error = type(exc).__name__
        yield
        if app.state.engine is not None:
            app.state.engine.dispose()

    app = FastAPI(title="SupplyLens", version=__version__, lifespan=lifespan,
                  description="采购到货分析与 Excel/CSV 导入；业务事实由确定性工具计算，来源逐单标记。")

    def service() -> AgentService:
        if app.state.service is None:
            raise HTTPException(503, "数据库初始化失败，请检查 DATABASE_URL 与服务状态。")
        return app.state.service

    @app.get("/health")
    async def health():
        database = "error"
        if app.state.engine is not None:
            try:
                def check():
                    with app.state.engine.connect() as connection:
                        connection.execute(text("SELECT 1"))
                await asyncio.to_thread(check)
                if app.state.startup_error is None:
                    database = "ok"
            except Exception:
                pass
        counts = await asyncio.to_thread(service().repo.source_counts) if database == "ok" else {}
        return {"status": "ok" if database == "ok" else "degraded", "database": database,
                "agent_mode": config.agent_mode, "embedding_mode": config.embedding_mode,
                "model_configured": bool(config.model_api_key), "as_of": config.as_of_date.isoformat(),
                "version": __version__, "boot_id": app.state.boot_id, "data_counts": counts}

    @app.get("/api/orders")
    async def orders(limit: int = Query(default=100, ge=1, le=1000), as_of: date | None = None):
        try:
            repo = service().repo if as_of is None else Repository(service().engine, as_of)
            items = await asyncio.to_thread(repo.list_orders, limit)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "订单数据库暂时不可用。") from None
        return {"items": items, "as_of": (as_of or config.as_of_date).isoformat()}

    @app.post("/api/chat", response_model=ChatResponse)
    async def chat(request: ChatRequest):
        if not request.message.strip():
            raise HTTPException(422, "message 不能为空白。")
        try:
            return await service().run(request.message.strip(), request.session_id, request.as_of)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "请求记录无法保存，请检查数据库连接后重试。") from None

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str):
        if len(run_id) > 64:
            raise HTTPException(404, "执行记录不存在。")
        try:
            result = await asyncio.to_thread(load_run, service().engine, run_id)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "执行记录数据库暂时不可用。") from None
        if result is None:
            raise HTTPException(404, "执行记录不存在。")
        return result

    @app.get("/api/imports/template")
    async def import_template(format: str = Query(default="xlsx", pattern="^(xlsx|csv)$")):
        path = ROOT / "data/templates" / ("supplylens-import-template.xlsx" if format == "xlsx" else "supplylens-import-csv.zip")
        if not path.is_file():
            raise HTTPException(503, "导入模板文件尚未准备好。")
        return FileResponse(path, filename=path.name)

    @app.post("/api/imports/preview")
    async def preview_import(files: list[UploadFile] = File(...), as_of: date = Form(...)):
        service()
        if len(files) > 6:
            raise HTTPException(413, "最多六个 CSV，或者一个 Excel 文件。")
        uploads = []
        try:
            for upload in files:
                body = await upload.read(MAX_FILE_BYTES + 1)
                if len(body) > MAX_FILE_BYTES:
                    raise HTTPException(413, "每个文件不能超过 5 MB。")
                uploads.append((upload.filename or "upload", body))
            return await asyncio.to_thread(app.state.import_service.preview, uploads, as_of)
        except ImportConflict as exc:
            raise HTTPException(409, {"errors": exc.errors}) from None
        except ImportValidationError as exc:
            raise HTTPException(422, {"errors": exc.errors}) from None
        finally:
            for upload in files:
                await upload.close()

    @app.post("/api/imports/commit")
    async def commit_import(request: ImportCommitRequest):
        service()
        if not request.confirm_snapshot:
            raise HTTPException(422, "请确认源文件覆盖范围、分析日期和更新订单后再导入。")
        try:
            result = await asyncio.to_thread(app.state.import_service.commit, request.preview_id)
            app.state.service.sessions.clear()
            return result
        except ImportConflict as exc:
            raise HTTPException(409, {"errors": exc.errors}) from None
        except ImportValidationError as exc:
            raise HTTPException(422, {"errors": exc.errors}) from None

        except SQLAlchemyError:
            raise HTTPException(503, "导入事务未完成，已回滚；请检查数据库后重新确认。") from None

    def provider_settings():
        # A local .env may be filled after startup; probes can re-read it safely.
        return config if settings is not None else Settings()

    @app.get("/api/cloud/status")
    async def provider_status():
        fresh = provider_settings()
        result = cloud_status(fresh)
        result["restart_required"] = any(getattr(fresh, key) != getattr(config, key) for key in (
            "model_api_key", "model_base_url", "model_name", "model_extra_body", "agent_mode", "embedding_mode"))
        return result

    @app.post("/api/cloud/check")
    async def provider_check():
        return await check_cloud(provider_settings())

    return app


app = create_app()

