import argparse
import sys
from collections.abc import Sequence

from ops_ai.config import load_ops_ai_settings
from ops_ai.db import make_engine, make_session_factory
from ops_ai.search.backfill import backfill_embeddings
from ops_ai.search.embedder import Embedder, LocalHashEmbedder
from ops_ai.search.openai_embedder import OpenAiEmbedder


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m ops_ai.search backfill --embedder local|openai`: embeds documents lacking a vector."""
    parser = argparse.ArgumentParser(prog="python -m ops_ai.search")
    commands = parser.add_subparsers(dest="command", required=True)
    backfill = commands.add_parser("backfill")
    backfill.add_argument("--embedder", choices=("local", "openai"), default="local")
    args = parser.parse_args(argv)
    settings = load_ops_ai_settings()
    embedder: Embedder = LocalHashEmbedder()
    if args.embedder == "openai":
        api_key = settings.openai_api_key
        if api_key is None or not api_key.get_secret_value():
            print("OPENAI_API_KEY is required for --embedder openai", file=sys.stderr)
            return 2
        embedder = OpenAiEmbedder(api_key, base_url=settings.openai_base_url)
    session_factory = make_session_factory(make_engine(settings.database_url))
    with session_factory() as session:
        filled = backfill_embeddings(session, embedder)
    print(f"backfilled {filled} documents for {embedder.embedder_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
