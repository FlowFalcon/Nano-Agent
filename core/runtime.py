"""Runtime/environment detection helpers for first-run setup.

The detector intentionally returns *hints*, not security decisions.  Use it to
choose a friendly first-run UX (interactive wizard, panel wizard, or manual
configuration), but keep dangerous permissions controlled by config.json.
"""

from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

RuntimeProfile = Literal[
    "local_interactive",
    "headless_server",
    "docker_container",
    "pterodactyl",
    "serverless",
    "unknown",
]


@dataclass(frozen=True)
class RuntimeEnvironment:
    """Detected runtime hints."""

    profile: RuntimeProfile
    os_name: str
    is_linux: bool
    is_interactive: bool
    is_headless: bool
    is_root: bool
    is_container: bool
    is_docker: bool
    is_pterodactyl: bool
    is_serverless: bool
    home: str
    cwd: str
    reason: str


def _file_contains(path: str, needles: tuple[str, ...]) -> bool:
    try:
        text = Path(path).read_text(errors="ignore").lower()
    except OSError:
        return False
    return any(needle in text for needle in needles)


def _env_any(*names: str) -> bool:
    return any(bool(os.environ.get(name)) for name in names)


def detect_runtime_environment() -> RuntimeEnvironment:
    """Detect common deployment targets.

    The return value is heuristic.  Pterodactyl and Hugging Face Spaces expose
    reasonably distinctive environment variables, but Docker/VPS/headless
    detection is inherently best-effort.
    """

    env = os.environ
    os_name = platform.system().lower()
    is_linux = os_name == "linux"

    stdin_tty = getattr(sys.stdin, "isatty", lambda: False)()
    stdout_tty = getattr(sys.stdout, "isatty", lambda: False)()
    is_interactive = bool(stdin_tty and stdout_tty)

    has_display = bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))
    is_headless = bool(is_linux and not has_display)

    is_root = bool(hasattr(os, "geteuid") and os.geteuid() == 0)

    cgroup_container = _file_contains(
        "/proc/1/cgroup",
        ("docker", "containerd", "kubepods", "podman", "libpod", "lxc"),
    )
    is_docker = bool(
        Path("/.dockerenv").exists()
        or cgroup_container
        or env.get("container", "").lower() in {"docker", "podman", "oci"}
    )
    is_container = bool(is_docker or Path("/run/.containerenv").exists() or cgroup_container)

    home = str(Path.home())
    is_pterodactyl = bool(
        env.get("P_SERVER_UUID")
        or env.get("P_SERVER_LOCATION")
        or (env.get("SERVER_MEMORY") and env.get("SERVER_PORT") and Path("/home/container").exists())
        or (env.get("USER") == "container" and home == "/home/container")
        or (Path("/home/container").exists() and is_container and env.get("SERVER_PORT"))
    )

    # Hugging Face Spaces and common serverless runtimes.  Keep this broad but
    # conservative: these environments often have non-persistent filesystems or
    # expect secrets/configuration to be supplied externally.
    is_huggingface_space = _env_any(
        "SPACE_ID",
        "SPACE_AUTHOR_NAME",
        "SPACE_REPO_NAME",
        "SPACE_HOST",
        "HF_SPACE_ID",
    )
    is_serverless = bool(
        is_huggingface_space
        or _env_any(
            "VERCEL",
            "AWS_LAMBDA_FUNCTION_NAME",
            "K_SERVICE",  # Cloud Run / Functions
            "FUNCTION_TARGET",
            "FUNCTION_NAME",
            "NETLIFY",
            "CF_PAGES",
            "RAILWAY_ENVIRONMENT",
            "RENDER",
        )
    )

    profile: RuntimeProfile
    reason: str
    if is_serverless:
        profile = "serverless"
        reason = "serverless/Hugging Face style environment variables detected"
    elif is_pterodactyl:
        profile = "pterodactyl"
        reason = "Pterodactyl/container panel environment hints detected"
    elif is_container:
        profile = "docker_container"
        reason = "container runtime hints detected"
    elif is_interactive:
        profile = "headless_server" if is_headless else "local_interactive"
        reason = "interactive terminal detected"
    elif is_headless:
        profile = "headless_server"
        reason = "headless Linux environment detected"
    else:
        profile = "unknown"
        reason = "no strong runtime hints detected"

    return RuntimeEnvironment(
        profile=profile,
        os_name=os_name,
        is_linux=is_linux,
        is_interactive=is_interactive,
        is_headless=is_headless,
        is_root=is_root,
        is_container=is_container,
        is_docker=is_docker,
        is_pterodactyl=is_pterodactyl,
        is_serverless=is_serverless,
        home=home,
        cwd=str(Path.cwd()),
        reason=reason,
    )


def format_runtime_summary(runtime: RuntimeEnvironment) -> str:
    """Return a concise human-readable runtime summary."""

    return (
        f"profile={runtime.profile}, os={runtime.os_name}, "
        f"interactive={runtime.is_interactive}, headless={runtime.is_headless}, "
        f"container={runtime.is_container}, pterodactyl={runtime.is_pterodactyl}, "
        f"serverless={runtime.is_serverless}, root={runtime.is_root}"
    )


def print_manual_config_help(config_path: Path) -> None:
    """Print manual setup instructions for serverless/non-interactive hosts."""

    print()
    print("[CONFIG REQUIRED]")
    print(f"No config file was found at: {config_path.resolve()}")
    print()
    print("This environment looks serverless or non-interactive, so the setup wizard is disabled.")
    print("Create config.json manually, or provide AGENT_CONFIG_PATH pointing to a config file.")
    print()
    print("Recommended environment/secrets to prepare:")
    print("  TELEGRAM_BOT_TOKEN")
    print("  ALLOWED_USER_IDS")
    print("  LLM_API_KEY")
    print("  LLM_BASE_URL")
    print("  LLM_MODEL")
    print()
    print("You can copy migrations/config.v6.example.json as a starting point.")
    print()
