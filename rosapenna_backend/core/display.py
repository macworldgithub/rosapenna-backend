"""Optional virtual display and VNC lifecycle for headed Chromium."""

import os
import subprocess
from typing import Optional

from rosapenna_backend.config import settings
from rosapenna_backend.core.logging import logger

_display_handle = None
_vnc_process: Optional[subprocess.Popen] = None

def start_virtual_display():
    global _display_handle
    if not settings.use_virtual_display:
        return
    if os.environ.get("DISPLAY"):
        logger.info("DISPLAY=%s already set, skipping virtual display startup", os.environ["DISPLAY"])
        return
    try:
        from pyvirtualdisplay import Display
        w, h = (int(x) for x in settings.virtual_display_size.lower().split("x"))
        _display_handle = Display(visible=0, size=(w, h))
        _display_handle.start()
        logger.info("Started virtual display %s at DISPLAY=%s", settings.virtual_display_size, os.environ.get("DISPLAY"))
    except Exception as e:
        logger.warning(
            "Could not start virtual display (install xvfb + pyvirtualdisplay for headed browsers "
            "on a VPS with no monitor): %s", e
        )

def stop_virtual_display():
    global _display_handle
    if _display_handle is not None:
        try:
            _display_handle.stop()
        except Exception:
            pass
        _display_handle = None

def start_vnc_server():
    global _vnc_process
    if not settings.enable_vnc:
        return
    display = os.environ.get("DISPLAY")
    if not display:
        logger.warning("BOOKING_ENABLE_VNC=true but no DISPLAY is set; skipping VNC startup")
        return
    cmd = ["x11vnc", "-display", display, "-forever", "-shared", "-rfbport", str(settings.vnc_port)]
    if settings.vnc_password:
        cmd += ["-passwd", settings.vnc_password]
    else:
        cmd += ["-nopw"]
    try:
        _vnc_process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger.info("Started x11vnc on display %s, port %s", display, settings.vnc_port)
    except FileNotFoundError:
        logger.warning("x11vnc binary not found; install it (apt install x11vnc) to enable remote viewing")
    except Exception as e:
        logger.warning("Could not start x11vnc: %s", e)

def stop_vnc_server():
    global _vnc_process
    if _vnc_process is not None:
        try:
            _vnc_process.terminate()
        except Exception:
            pass
        _vnc_process = None
