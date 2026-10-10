"""
Desktop notifications, with no extra dependencies.

Windows uses the built-in WinRT toast API through PowerShell, macOS uses
``osascript``, Linux uses ``notify-send``. Text is passed through environment
variables, never interpolated into a script, so reminder text cannot inject
commands. Every notification is also appended to ``notifications.log`` in the
runtime data directory, so nothing is lost if the desktop popup fails.
"""

import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from friday.config import ROOT_DIR

log = logging.getLogger(__name__)

NOTIFY_LOG = ROOT_DIR / "data" / "notifications.log"

_PS_TOAST = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$texts = $template.GetElementsByTagName('text')
$texts.Item(0).AppendChild($template.CreateTextNode($env:FRIDAY_NOTIFY_TITLE)) | Out-Null
$texts.Item(1).AppendChild($template.CreateTextNode($env:FRIDAY_NOTIFY_BODY)) | Out-Null
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show([Windows.UI.Notifications.ToastNotification]::new($template))
"""

_OSASCRIPT = (
    'display notification (system attribute "FRIDAY_NOTIFY_BODY") '
    'with title (system attribute "FRIDAY_NOTIFY_TITLE")'
)


def _log_line(title: str, body: str, path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now().isoformat(timespec='seconds')}\t{title}\t{body}\n")
    except OSError as exc:
        log.warning("Could not write notification log: %s", exc)


def desktop_command() -> list[str] | None:
    """The OS command used for popups, or None when unavailable."""
    if sys.platform == "win32":
        exe = shutil.which("powershell") or shutil.which("pwsh")
        return [exe, "-NoProfile", "-NonInteractive", "-Command", _PS_TOAST] if exe else None
    if sys.platform == "darwin":
        exe = shutil.which("osascript")
        return [exe, "-e", _OSASCRIPT] if exe else None
    exe = shutil.which("notify-send")
    return [exe, "--app-name=Friday", "{title}", "{body}"] if exe else None


def notify(title: str, body: str, desktop: bool = True, log_path: Path | None = None) -> bool:
    """Show a notification. Returns True if a desktop popup was shown."""
    _log_line(title, body, log_path or NOTIFY_LOG)
    if not desktop:
        return False
    cmd = desktop_command()
    if not cmd:
        return False
    # notify-send takes text as argv (no shell, so still safe); others read env.
    argv = [title if a == "{title}" else body if a == "{body}" else a for a in cmd]
    env = {**os.environ, "FRIDAY_NOTIFY_TITLE": title, "FRIDAY_NOTIFY_BODY": body}
    try:
        subprocess.run(argv, env=env, capture_output=True, timeout=15, check=True)
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("Desktop notification failed: %s", exc)
        return False
