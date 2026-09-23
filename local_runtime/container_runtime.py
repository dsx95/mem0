"""Container factory: load a profile from a read-only configuration directory."""

import os

from .dashboard import create_app as create_dashboard
from .runtime import load_settings


def create_app():
    profile = os.environ.get("MEM0_ENV_FILE", "/run/mem0-config/qwen.env")
    return create_dashboard(settings=load_settings(profile))
