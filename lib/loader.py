"""The bubble loader an app keeps in its static files, made from the project's registered addresses.

It loads the bubble only on those exact addresses (ports and Caddy `.localhost` names alike), so one copy
works in a build's Development and Live, and tests (served on other ports) never load it. The file says
it is generated: register the addresses, then write it again; never edit the copies by hand.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from lib import paths
from lib.registry import Project

logger = logging.getLogger(__name__)

MARK = "bubble-fix bubble loader"
TEMPLATE = """/* {mark} for {name}, made by `bubble_fix.py loader --project {name}`
 * (bubble-fix). Generated: register the app's addresses, then write it again.
 *
 * Loads the bubble only on this build's own addresses, where the collector runs on {api}.
 * Tests serve the app on other ports, so it never loads there.
 */
(() => {{
  "use strict";
  const ORIGINS = {origins};
  if (!ORIGINS.includes(window.location.origin) || document.querySelector("script[data-bubble-fix], script[data-fix-heal-loop]")) return;
  const script = document.createElement("script");
  script.src = "{api}/bubble.js";
  script.defer = true;
  script.dataset.bubbleFix = "1";
  document.head.append(script);
}})();
"""


def render(project: Project) -> str:
    """The loader for this project's registered addresses."""
    if not project.origins:
        raise ValueError(f"{project.name} has no app addresses; register some first")
    api = f"http://{paths.DEFAULT_HOST}:{paths.DEFAULT_PORT}"
    return TEMPLATE.format(mark=MARK, name=project.name, api=api, origins=json.dumps(project.origins))


def write(project: Project, target: Path) -> Path:
    """Write the loader to `target` (an app's static file). Refuses to replace a file that is not a loader."""
    if target.exists() and not any(m in target.read_text(errors="replace") for m in ("bubble-fix bubble", "fix-heal-loop bubble")):  # this loader or a hand-made one
        raise ValueError(f"{target} is not a bubble-fix loader; not replacing it")
    if not target.parent.is_dir():
        raise ValueError(f"{target.parent} is not a folder")
    target.write_text(render(project))
    target.chmod(0o644)  # a static file the app serves
    logger.info("wrote the %s loader to %s", project.name, target)
    return target
