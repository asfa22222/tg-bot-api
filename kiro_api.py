"""Kiro CLI integration via headless mode."""

import asyncio
import logging
import shutil

from config import KIRO_API_KEY

logger = logging.getLogger(__name__)

KIRO_CLI_TIMEOUT = 120


class KiroError(Exception):
    pass


class KiroNotConfiguredError(KiroError):
    pass


def _find_kiro_cli() -> str:
    """Locate kiro-cli binary on PATH or common install locations."""
    path = shutil.which("kiro-cli")
    if path:
        return path
    for candidate in (
        "/usr/local/bin/kiro-cli",
        "/home/appuser/.local/bin/kiro-cli",
        "/root/.local/bin/kiro-cli",
    ):
        if shutil.which(candidate):
            return candidate
    raise KiroError(
        "kiro-cli not found. Install: curl -fsSL https://cli.kiro.dev/install | bash"
    )


async def send_prompt(prompt: str) -> str:
    """Run a prompt through kiro-cli in headless mode.

    Returns the text output from the agent.
    """
    if not KIRO_API_KEY:
        raise KiroNotConfiguredError(
            "KIRO_API_KEY not set. Add it to environment variables."
        )

    kiro_bin = _find_kiro_cli()

    cmd = [
        kiro_bin,
        "chat",
        "--no-interactive",
        "--print",
        prompt,
    ]

    env = {"KIRO_API_KEY": KIRO_API_KEY, "PATH": "/usr/local/bin:/usr/bin:/bin"}

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=KIRO_CLI_TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise KiroError("Kiro CLI timed out (120s).")
    except FileNotFoundError:
        raise KiroError("kiro-cli binary not found.")

    if proc.returncode != 0:
        err_text = stderr.decode(errors="replace").strip()
        logger.error("kiro-cli exited %d: %s", proc.returncode, err_text)
        raise KiroError(f"kiro-cli error: {err_text or 'unknown error'}")

    output = stdout.decode(errors="replace").strip()
    if not output:
        output = "(empty response from Kiro)"

    return output
