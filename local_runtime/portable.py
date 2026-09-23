"""Project-local uv entrypoint; no external database server or container required."""

import argparse
import getpass
import json
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def configure():
    # The portable distribution owns these directories, independent of inherited shell settings.
    os.environ["MEM0_DATA_DIR"] = str(ROOT / "data")
    os.environ["MEM0_DIR"] = str(ROOT / "data/sdk")
    os.environ["FASTEMBED_CACHE_PATH"] = str(ROOT / "data/models/fastembed")
    os.environ["MEM0_PORTABLE_ROOT"] = str(ROOT)
    os.environ.setdefault("MEM0_TELEMETRY", "false")
    for name in ["data", "materials", "config"]:
        (ROOT / name).mkdir(parents=True, exist_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=["qwen", "openai", "local", "ollama"], default="qwen")
    parser.add_argument("--port", type=int, default=18580)
    parser.add_argument("--doctor", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1–65535")
    configure()
    if args.doctor:
        import importlib
        import spacy

        for module in ("av", "fitz", "fastembed", "qdrant_client"):
            importlib.import_module(module)
        print(
            json.dumps(
                {
                    "root": str(ROOT),
                    "python_dependencies": "ok",
                    "spacy_model": spacy.util.is_package("en_core_web_sm"),
                    "libreoffice": shutil.which("soffice") or shutil.which("libreoffice"),
                    "data": str(ROOT / "data"),
                    "materials": str(ROOT / "materials"),
                    "network_model_check": "not requested",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    profile = ROOT / "config" / (args.profile + ".env")
    if not profile.exists():
        with profile.open("x", opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
            stream.write(profile.with_suffix(".env.example").read_text())
    from dotenv import dotenv_values

    values = dotenv_values(profile, interpolate=False)
    if args.profile in ("qwen", "openai") and not (
        values.get("MEM0_PROVIDER_API_KEY") or os.environ.get("MEM0_PROVIDER_API_KEY")
    ):
        import sys

        if not sys.stdin.isatty():
            parser.error(f"请填写 {profile} 中的 MEM0_PROVIDER_API_KEY，再执行启动命令。")
        key = getpass.getpass(f"{args.profile} API Key（隐藏输入）: ").strip()
        if not key or any(c in key for c in "\n\r\x00"):
            parser.error("API Key 不能为空或包含换行")
        from dotenv import set_key

        profile.chmod(0o600)
        set_key(str(profile), "MEM0_PROVIDER_API_KEY", key)
    from .runtime import load_settings

    settings = load_settings(profile)
    settings.validate()
    from .dashboard import create_app
    import uvicorn

    print(f"记忆服务：http://127.0.0.1:{args.port} | 数据目录：{settings.data_dir}", flush=True)
    uvicorn.run(create_app(settings, root=ROOT / "materials"), host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
