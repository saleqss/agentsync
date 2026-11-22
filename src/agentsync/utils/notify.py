"""Optional cross-platform desktop notifications (opt-in, silent no-op by default)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


def send_desktop_notification(title: str, message: str, *, enabled: bool = False) -> bool:
    """Send a subtle non-blocking OS notification when explicitly enabled.

    Returns True if a platform notifier was dispatched, False when disabled, headless, or unavailable.
    """
    if not enabled:
        return False
    if os.environ.get("CI") or os.environ.get("PYTEST_CURRENT_TEST"):
        return False

    clean_title = title.replace('"', "'").replace("\n", " ").strip()[:80]
    clean_msg = message.replace('"', "'").replace("\n", " ").strip()[:200]

    try:
        if sys.platform == "win32":
            ps_script = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
                "ContentType = WindowsRuntime] | Out-Null; "
                "$tpl = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
                "[Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
                "$nodes = $tpl.GetElementsByTagName('text'); "
                f"$nodes.Item(0).AppendChild($tpl.CreateTextNode(\"{clean_title}\")) | Out-Null; "
                f"$nodes.Item(1).AppendChild($tpl.CreateTextNode(\"{clean_msg}\")) | Out-Null; "
                "$toast = [Windows.UI.Notifications.ToastNotification]::new($tpl); "
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('AgentSync').Show($toast)"
            )
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps_script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            return True
        if sys.platform == "darwin":
            subprocess.Popen(
                ["osascript", "-e", f'display notification "{clean_msg}" with title "{clean_title}"'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        if shutil.which("notify-send"):
            subprocess.Popen(
                ["notify-send", clean_title, clean_msg],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
    except Exception:
        return False
    return False
