import argparse
import asyncio
import json
from pathlib import Path

from supplylens.api import prepare_sqlite_dir
from supplylens.config import ROOT, Settings
from supplylens.db import init_database, make_engine
from supplylens.retrieval import SOPIndex


def main():
    parser = argparse.ArgumentParser(description="SupplyLens 模拟采购到货分析")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="建表并幂等导入模拟数据")
    sub.add_parser("index", help="构建与当前检索模式匹配的 SOP 索引")
    serve = sub.add_parser("serve", help="启动 FastAPI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    evaluate = sub.add_parser("evaluate", help="运行独立标准答案的端到端评测")
    evaluate.add_argument("--mode", choices=["demo", "cloud"], default="demo")
    evaluate.add_argument("--cases", type=Path, default=ROOT / "evaluation/cases.json")
    evaluate.add_argument("--output", type=Path, default=ROOT / "evaluation/results/demo.json")
    demo = sub.add_parser("demo", help="运行三段演示并保存真实 API 格式结果和 Markdown")
    demo.add_argument("--output", type=Path, default=ROOT / "docs/demos")
    args = parser.parse_args()
    settings = Settings()
    if args.command == "init":
        prepare_sqlite_dir(settings.database_url)
        engine = make_engine(settings.database_url)
        try:
            init_database(engine, seed=True)
            print("数据库已初始化，模拟数据已幂等导入。")
        finally:
            engine.dispose()
    elif args.command == "index":
        print(json.dumps(SOPIndex(settings).build(), ensure_ascii=False, indent=2))
    elif args.command == "serve":
        import uvicorn
        uvicorn.run("supplylens.api:create_app", factory=True, host=args.host, port=args.port)
    elif args.command == "evaluate":
        from supplylens.evaluation import evaluate_cases
        result = asyncio.run(evaluate_cases(settings, args.mode, args.cases, args.output))
        print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
        raise SystemExit(0 if result["summary"]["passed"] == result["summary"]["total"] else 1)
    elif args.command == "demo":
        from supplylens.agent import AgentService
        prepare_sqlite_dir(settings.database_url)
        engine = make_engine(settings.database_url)
        init_database(engine)
        service = AgentService(engine, settings)
        args.output.mkdir(parents=True, exist_ok=True)

        async def run_demos():
            first = await service.run("分析采购单 PO-1001，为什么还没到齐？")
            followup = await service.run("帮我写一份供应商跟进建议。", first["session_id"])
            missing = await service.run("分析 PO-1009，能确认整体正常吗？")
            return [first, followup, missing]

        try:
            for name, result in zip(["01-overdue", "02-followup", "03-missing-data"], asyncio.run(run_demos())):
                (args.output / f"{name}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                (args.output / f"{name}.md").write_text(result["markdown"], encoding="utf-8")
            print(f"三段实际执行结果已保存到 {args.output}")
        finally:
            engine.dispose()


if __name__ == "__main__":
    main()

