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


def hold_seconds(value):
    try: seconds=int(value)
    except ValueError: raise argparse.ArgumentTypeError('Enter a whole number of seconds')
    if not 1<=seconds<=86400: raise argparse.ArgumentTypeError('Hold duration must be between 1 and 86400 seconds')
    return seconds


def main(argv=None):
    parser=argparse.ArgumentParser(prog="vocabatron",description="本地词表、冻结线索、双版本填字与原模板 PDF 验证")
    parser.add_argument("--private-dir",type=Path,default=Path(".private"))
    parser.add_argument('--app-config',type=Path)
    parser.add_argument('--lesson-id')
    commands=parser.add_subparsers(dest="command",required=True)
    ingest=commands.add_parser('ingest');ingest.add_argument('--source',type=Path)
    for name in ('check-transcript','metrics','library','restore'):commands.add_parser(name)
    select=commands.add_parser("select");select.add_argument("--new-version",action="store_true")
    generate=commands.add_parser("generate");generate.add_argument("--task-id");generate.add_argument("--seconds",type=float);generate.add_argument("--workers",type=int)
    generate.add_argument('--count',type=int);generate.add_argument('--all',action='store_true')
    for name in ("verify","status","cancel","pause","resume","retry"):
        p=commands.add_parser(name);p.add_argument("task_id")
    rebuild=commands.add_parser("rebuild");rebuild.add_argument("task_id");rebuild.add_argument("--new-task-id",required=True)
    acceptance=commands.add_parser("private-test");acceptance.add_argument("task_id");acceptance.add_argument("--allow-private-integration",action="store_true",required=True)
    privacy=commands.add_parser("privacy-scan");privacy.add_argument("--project",type=Path,default=Path("."));privacy.add_argument("--packages",type=Path)
    benchmark=commands.add_parser("benchmark-model");benchmark.add_argument("--allow-private-integration",action="store_true",required=True);benchmark.add_argument("--sample-words",type=int,default=1)
    hold=commands.add_parser('hold');hold.add_argument('--seconds',type=hold_seconds,default=600)
    for name in ('hold-status','release-hold'):
        item=commands.add_parser(name);item.add_argument('hold_id')
    args=parser.parse_args(argv)
    try:
        if args.command=="privacy-scan":
            from .privacy import scan
            result=scan(args.project,args.packages)
        else:
            from .app.cli import execute
            result=execute(args)
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
