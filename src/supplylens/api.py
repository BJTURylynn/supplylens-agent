import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query
from sqlalchemy import text

from supplylens import __version__
from supplylens.agent import AgentService
from supplylens.config import Settings
from supplylens.db import init_database, load_run, make_engine
from supplylens.schemas import ChatRequest, ChatResponse


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
        except Exception as exc:
            app.state.startup_error = type(exc).__name__
        yield
        if app.state.engine is not None:
            app.state.engine.dispose()

    app = FastAPI(title="SupplyLens", version=__version__, lifespan=lifespan,
                  description="模拟汽车零部件采购到货分析；所有业务事实来自确定性工具。")

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
        return {"status": "ok" if database == "ok" else "degraded", "database": database,
                "agent_mode": config.agent_mode, "embedding_mode": config.embedding_mode,
                "model_configured": bool(config.model_api_key), "as_of": config.as_of_date.isoformat(),
                "version": __version__, "boot_id": app.state.boot_id}

    @app.get("/api/orders")
    async def orders(limit: int = Query(default=100, ge=1, le=1000)):
        try:
            items = await asyncio.to_thread(service().repo.list_orders, limit)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "订单数据库暂时不可用。") from None
        return {"items": items, "as_of": config.as_of_date.isoformat()}

    @app.post("/api/chat", response_model=ChatResponse)
    async def chat(request: ChatRequest):
        if not request.message.strip():
            raise HTTPException(422, "message 不能为空白。")
        try:
            return await service().run(request.message.strip(), request.session_id)
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

    return app


app = create_app()

