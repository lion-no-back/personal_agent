import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

from .ingester.pipeline import Pipeline
from .store import KBStore
from .watcher import WATCH_EXTENSIONS, KBWatcher

DB_PATH = Path("knowledge_docs/kb.sqlite")
WIKI_DIR = Path("knowledge_docs/wiki")
SOURCES_DIR = Path("knowledge_docs/sources")


def _build_pipeline() -> tuple[KBStore, Pipeline]:
    from personal_agent.core.llm_router import LLMRouter

    store = KBStore(DB_PATH)
    llm = LLMRouter()
    return store, Pipeline(store=store, llm=llm, wiki_dir=WIKI_DIR)


# ── subcommand handlers ────────────────────────────────────────────


def cmd_ingest(args: argparse.Namespace) -> None:
    store, pipeline = _build_pipeline()

    if args.all:
        SOURCES_DIR.mkdir(parents=True, exist_ok=True)
        paths = [
            p for p in SOURCES_DIR.rglob("*")
            if p.is_file() and p.suffix.lower() in WATCH_EXTENSIONS
        ]
        if not paths:
            print(f"No supported files found in {SOURCES_DIR}")
            return
        asyncio.run(pipeline.ingest_batch(paths, force=args.force))
    else:
        path = Path(args.path)
        result = asyncio.run(pipeline.ingest(path, force=args.force))
        status = "SKIP" if result.skipped else "OK"
        print(f"[{status}] {path} → {result}")


def cmd_status(_args: argparse.Namespace) -> None:
    KBStore(DB_PATH).print_stats()


def cmd_watch(_args: argparse.Namespace) -> None:
    # Pre-warm PaddleOCR deps (install + fix torch DLL) before entering the event loop,
    # so the first image file doesn't trigger a blocking 200 MB download mid-ingest.
    from personal_agent.knowledge.paddle_bootstrap import ensure_paddle
    ensure_paddle()

    store, pipeline = _build_pipeline()
    SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    watcher = KBWatcher(pipeline=pipeline, store=store)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    watcher.start(loop, SOURCES_DIR)
    print(f"Watching {SOURCES_DIR} … (Ctrl-C to stop)")
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        watcher.stop()
        loop.close()


# ── entry point ────────────────────────────────────────────────────


def main() -> None:
    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
    parser = argparse.ArgumentParser(prog="python -m personal_agent.knowledge")
    sub = parser.add_subparsers(dest="command")

    p_ingest = sub.add_parser("ingest", help="Ingest files into the knowledge base")
    grp = p_ingest.add_mutually_exclusive_group(required=True)
    grp.add_argument("--all", action="store_true", help=f"Ingest all files under {SOURCES_DIR}")
    grp.add_argument("--path", metavar="FILE", help="Ingest a single file")
    p_ingest.add_argument("--force", action="store_true", help="Re-ingest even if hash is unchanged")

    sub.add_parser("status", help="Show knowledge base statistics")
    sub.add_parser("watch", help=f"Watch {SOURCES_DIR} and auto-ingest on change")

    args = parser.parse_args()

    dispatch = {"ingest": cmd_ingest, "status": cmd_status, "watch": cmd_watch}
    fn = dispatch.get(args.command)
    if fn:
        fn(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
