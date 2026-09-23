"""CLI diagnostics and explicit model/memory calls. `config` never calls a model."""

import argparse
import json
import sys

from .runtime import close_memory, create_embedder, create_llm, create_memory, load_settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", help="Profile path; default: project/.env")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("config", help="Print redacted configuration without making requests")
    check = commands.add_parser("check", help="Call the selected real endpoints to check connectivity")
    check.add_argument("--component", choices=["all", "llm", "embedding"], default="all")
    llm = commands.add_parser("llm", help="Call the configured LLM without opening a memory database")
    llm.add_argument("text")
    for name in ("add", "search"):
        command = commands.add_parser(name)
        command.add_argument("text")
        command.add_argument("--user-id", required=True)
        if name == "search":
            command.add_argument("--top-k", type=int, default=5)
        else:
            command.add_argument(
                "--raw", action="store_true", help="Save supplied fact directly; still calls embeddings"
            )
    args = parser.parse_args()
    try:
        settings = load_settings(args.env_file)
        if args.command == "config":
            result = settings.describe()
        elif args.command == "llm":
            result = create_llm(settings).generate_response(messages=[{"role": "user", "content": args.text}])
        elif args.command == "check":
            result = {}
            if args.component in {"llm", "all"}:
                response = create_llm(settings).generate_response(
                    messages=[{"role": "user", "content": 'Return only this JSON object: {"ok": true}'}],
                    response_format={"type": "json_object"},
                )
                if not isinstance(json.loads(response), dict):
                    raise ValueError("LLM response must be a JSON object for memory extraction")
                result["llm"] = "reachable; JSON response validated"
            if args.component in {"embedding", "all"}:
                vectors = create_embedder(settings).embed_batch(["记忆接口测试", "embedding check"])
                if len(vectors) != 2 or any(len(v) != settings.embedding_dims for v in vectors):
                    raise ValueError("Embedding output dimension/count does not match MEM0_EMBEDDING_DIMS")
                result["embedding"] = {"status": "reachable", "dimensions": settings.embedding_dims, "batch_size": 2}
        else:
            memory = create_memory(settings)
            try:
                if args.command == "add":
                    result = memory.add(args.text, user_id=args.user_id, infer=not args.raw)
                else:
                    result = memory.search(args.text, filters={"user_id": args.user_id}, top_k=args.top_k)
            finally:
                close_memory(memory)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except ValueError as exc:
        print(f"Configuration/response error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        # Provider error bodies can echo tokens or prompts. Report type/status only.
        status = getattr(exc, "status_code", None)
        print(f"Request failed: {type(exc).__name__}" + (f" (HTTP {status})" if status else ""), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
