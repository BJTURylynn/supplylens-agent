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
    cloud = sub.add_parser("check-cloud", help="真实聊天工具调用与向量接口联调，仅发送测试文本")
    cloud.add_argument("--output", type=Path, default=Path("runtime/cloud-check.json"))
    imports = sub.add_parser("import-data", help="校验并导入 Excel/CSV 采购快照")
    imports.add_argument("files", nargs="+", type=Path)
    imports.add_argument("--as-of", required=True, help="源文件快照日期 YYYY-MM-DD")
    imports.add_argument("--commit", action="store_true", help="确认源文件覆盖范围并实际导入；默认仅预览")
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
    elif args.command == "check-cloud":
        from supplylens.cloud import check_cloud
        result = asyncio.run(check_cloud(settings))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(0 if result["status"] == "ok" else 1)
    elif args.command == "import-data":
        from datetime import date

        from supplylens.imports import MAX_FILE_BYTES, MAX_TOTAL_BYTES, ImportService, ImportValidationError
        prepare_sqlite_dir(settings.database_url)
        engine = make_engine(settings.database_url)
        try:
            init_database(engine, seed=settings.auto_seed)
            importer = ImportService(engine)
            if len(args.files) > 6 or any(path.stat().st_size > MAX_FILE_BYTES for path in args.files) or sum(path.stat().st_size for path in args.files) > MAX_TOTAL_BYTES:
                raise ImportValidationError(["文件数量或大小超限：最多六个文件，每个 5 MB，合计 20 MB。"])
            preview = importer.preview([(path.name, path.read_bytes()) for path in args.files], date.fromisoformat(args.as_of))
            print(json.dumps(preview, ensure_ascii=False, indent=2))
            if args.commit:
                print(json.dumps(importer.commit(preview["preview_id"]), ensure_ascii=False, indent=2))
            else:
                print("尚未写入业务数据；确认后加 --commit 执行。")
        except ImportValidationError as exc:
            print(json.dumps({"errors": exc.errors}, ensure_ascii=False, indent=2))
            raise SystemExit(1) from None
        finally:
            engine.dispose()
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

