"""Small command surface; computation and persistence live in services."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from pydantic import ValidationError

from .domain import Lesson, Problem
from .ingest import check_transcription
from .storage import PrivateStore, identifier
from . import services


EXIT_CODES={"BUSY":5,"CANCELLED":130,"UNKNOWN":6,"INFEASIBLE":7,"MODEL_INVALID":8,"RESOURCE_LIMIT":9,"WORKER_FAILED":10,"PUBLISH_FAILED":11}


def main(argv=None):
    parser=argparse.ArgumentParser(prog="vocabatron",description="本地词表、冻结线索、双版本填字与原模板 PDF 验证")
    parser.add_argument("--private-dir",type=Path,default=Path(".private"))
    commands=parser.add_subparsers(dest="command",required=True)
    for name in ("ingest","check-transcript","metrics"):commands.add_parser(name)
    select=commands.add_parser("select");select.add_argument("--new-version",action="store_true")
    generate=commands.add_parser("generate");generate.add_argument("--task-id");generate.add_argument("--seconds",type=float);generate.add_argument("--workers",type=int)
    for name in ("verify","status","cancel"):
        p=commands.add_parser(name);p.add_argument("task_id")
    rebuild=commands.add_parser("rebuild");rebuild.add_argument("task_id");rebuild.add_argument("--new-task-id",required=True)
    acceptance=commands.add_parser("private-test");acceptance.add_argument("task_id");acceptance.add_argument("--allow-private-integration",action="store_true",required=True)
    privacy=commands.add_parser("privacy-scan");privacy.add_argument("--project",type=Path,default=Path("."));privacy.add_argument("--packages",type=Path)
    benchmark=commands.add_parser("benchmark-model");benchmark.add_argument("--allow-private-integration",action="store_true",required=True);benchmark.add_argument("--sample-words",type=int,default=1)
    args=parser.parse_args(argv)
    try:
        if args.command=="privacy-scan":
            from .privacy import scan
            result=scan(args.project,args.packages)
        else:
            private_root=Path(__file__).resolve().parents[2]/".private"
            if not args.private_dir.absolute().is_relative_to(private_root):raise Problem("UNSAFE_PATH","CLI 私有目录必须位于当前项目 .private 内")
            store=PrivateStore(args.private_dir)
            if args.command=="ingest":result=services.import_lesson(store)
            elif args.command=="check-transcript":
                result=services.check_lesson(store)
            elif args.command=="select":result=services.select_clues(store,new_version=args.new_version)
            elif args.command=="generate":
                cfg=services.configuration(store);values=cfg.solver.model_dump()
                if args.seconds is not None:values["seconds_per_layout"]=args.seconds
                if args.workers is not None:values["workers"]=args.workers
                options=type(cfg.solver).model_validate(values)
                result=services.generate(store,args.task_id,options,lambda stage:print(json.dumps({"stage":stage}),file=sys.stderr,flush=True))
            elif args.command=="verify":result=services.verify_set(store,args.task_id)
            elif args.command=="rebuild":result=services.rebuild(store,args.task_id,args.new_task_id)
            elif args.command=="private-test":result=services.private_acceptance(store,args.task_id)
            elif args.command=="status":result=services.task_status(store,args.task_id)
            elif args.command=="cancel":
                result=services.cancel_attempt(store,args.task_id)
            elif args.command=="metrics":
                _,lesson,frozen=services.load_inputs(store)
                result=services.verify_evidence(store,lesson,frozen)
            elif args.command=="benchmark-model":result=services.benchmark_model(store,args.sample_words)
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except Problem as e:
        print(json.dumps({"status":e.code,"message":e.message},ensure_ascii=False),file=sys.stderr)
        return EXIT_CODES.get(e.code,2)
    except (FileNotFoundError,PermissionError,ValidationError,json.JSONDecodeError) as e:
        print(json.dumps({"status":"INPUT_UNAVAILABLE_OR_INVALID","error_type":type(e).__name__},ensure_ascii=False),file=sys.stderr)
        return 3
    except Exception as e:
        # Never include source data or complete exception strings in default logs.
        print(json.dumps({"status":"INTERNAL_ERROR","error_type":type(e).__name__}),file=sys.stderr)
        return 70


if __name__=="__main__":raise SystemExit(main())
