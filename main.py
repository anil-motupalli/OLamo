"""OLamo — multi-agent software development pipeline."""
from app import *  # noqa: F401, F403  (backward compat for tests)
from app.web.app import create_app  # noqa: F401  (backward compat)


def main() -> None:
    import argparse, sys
    from pathlib import Path
    parser = argparse.ArgumentParser(description="OLamo development pipeline")
    parser.add_argument("task", nargs="*")
    parser.add_argument("--server", action="store_true")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--pr-url", default="")
    parser.add_argument("--settings", default=None)
    parser.add_argument("--headless", action="store_true",
                        help="Dry-run mode: use MockEngine, no real API calls made")
    args = parser.parse_args()
    settings_file = Path(args.settings) if args.settings else None
    if args.server:
        try:
            import uvicorn
        except ImportError:
            print("uvicorn not installed. Run: pip install uvicorn[standard]")
            sys.exit(1)
        print(f"Starting OLamo server on http://0.0.0.0:{args.port}")
        uvicorn.run(create_app(settings_file=settings_file), host="0.0.0.0", port=args.port)
        return
    import asyncio
    from app.pipeline.runner import run_pipeline_cli
    if args.task:
        task = " ".join(args.task)
    else:
        task = input("Describe the task for OLamo: ").strip()
        if not task:
            print("No task provided. Exiting.")
            sys.exit(1)
    asyncio.run(run_pipeline_cli(task, pr_url=args.pr_url, settings_file=settings_file, headless=args.headless))


if __name__ == "__main__":
    main()
