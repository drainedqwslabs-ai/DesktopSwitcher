"""
macOS-Style Desktop Switcher for Windows — with GUI
Animated background, Run/Stop toggle, tray integration, autostart.
"""

import sys
import json
import threading
import math
import os
from pathlib import Path

from PyQt5.QtCore import (
    Qt, QPropertyAnimation, QEasingCurve, QRect, QTimer,
    pyqtSignal, QObject, pyqtProperty, QPointF
)
from PyQt5.QtGui import (
    QPainter, QColor, QLinearGradient, QRadialGradient,
    QIcon, QPixmap, QBrush
)
from PyQt5.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QSystemTrayIcon, QMenu, QAction, QMessageBox, QFrame, QCheckBox
)
import keyboard
from pyvda import VirtualDesktop, get_virtual_desktops


# ---------- Configuration ----------
CONFIG_PATH = Path.home() / ".desktop_switcher_config.json"

DEFAULT_CONFIG = {
    "hotkey_right": "shift+windows+right",
    "hotkey_left": "shift+windows+left",
    "enabled": True,
}

AUTOSTART_FLAG = "--autostart"


def load_config():
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                data.setdefault(k, v)
            return data
        except Exception:
            return dict(DEFAULT_CONFIG)
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"Failed to save config: {e}", file=sys.stderr)


# ---------- Autostart (Windows Registry) ----------
import winreg

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "DesktopSwitcher"


def _get_exe_path():
    """Path to the running program (works for both .py and .exe)."""
    if getattr(sys, "frozen", False):
        return sys.executable
    else:
        pythonw = sys.executable.replace("python.exe", "pythonw.exe")
        script = os.path.abspath(sys.argv[0])
        return f'"{pythonw}" "{script}"'


def autostart_is_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
            return bool(value)
    except FileNotFoundError:
        return False
    except Exception:
        return False


def autostart_enable():
    exe = _get_exe_path()
    if getattr(sys, "frozen", False):
        cmd = f'"{exe}" {AUTOSTART_FLAG}'
    else:
        cmd = f'{exe} {AUTOSTART_FLAG}'
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
        return True
    except Exception as e:
        print(f"Failed to enable autostart: {e}", file=sys.stderr)
        return False


def autostart_disable():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, APP_NAME)
        return True
    except FileNotFoundError:
        return True
    except Exception as e:
        print(f"Failed to disable autostart: {e}", file=sys.stderr)
        return False


# ---------- Signal bridge (thread-safe) ----------
class HotkeyBridge(QObject):
    triggered = pyqtSignal(str)


# ---------- Swipe overlay ----------
class SwipeOverlay(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        screen_geo = QApplication.primaryScreen().geometry()
        self.screen_w = screen_geo.width()
        self.screen_h = screen_geo.height()
        self.setGeometry(screen_geo)

        self.direction = "right"
        self._offset = 0.0
        self.hide()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        gradient = QLinearGradient(0, 0, self.screen_w, 0)
        gradient.setColorAt(0.0, QColor(20, 20, 25, 0))
        gradient.setColorAt(0.5, QColor(20, 20, 25, 180))
        gradient.setColorAt(1.0, QColor(20, 20, 25, 0))

        band_width = int(self.screen_w * 0.6)
        if self.direction == "right":
            x = int(-band_width + (self.screen_w + band_width) * self._offset)
        else:
            x = int(self.screen_w - (self.screen_w + band_width) * self._offset)
        painter.fillRect(QRect(x, 0, band_width, self.screen_h), gradient)

    def get_offset(self):
        return self._offset

    def set_offset(self, value):
        self._offset = value
        self.update()

    offset_prop = pyqtProperty(float, get_offset, set_offset)

    def play(self, direction):
        self.direction = direction
        self._offset = 0.0
        self.setWindowOpacity(1.0)
        self.show()
        self.raise_()
        self.swipe_anim = QPropertyAnimation(self, b"offset_prop")
        self.swipe_anim.setDuration(350)
        self.swipe_anim.setStartValue(0.0)
        self.swipe_anim.setEndValue(1.0)
        self.swipe_anim.setEasingCurve(QEasingCurve.OutCubic)
        self.swipe_anim.finished.connect(self._fade_out)
        self.swipe_anim.start()

    def _fade_out(self):
        self.fade_anim = QPropertyAnimation(self, b"windowOpacity")
        self.fade_anim.setDuration(120)
        self.fade_anim.setStartValue(1.0)
        self.fade_anim.setEndValue(0.0)
        self.fade_anim.finished.connect(self.hide)
        self.fade_anim.start()


# ---------- Desktop switch ----------
def switch_desktop(direction):
    try:
        desktops = get_virtual_desktops()
        current = VirtualDesktop.current()
        current_index = next(
            (i for i, d in enumerate(desktops) if d.number == current.number), 0
        )
        if direction == "right":
            target = (current_index + 1) % len(desktops)
        else:
            target = (current_index - 1) % len(desktops)
        desktops[target].go()
    except Exception as e:
        print(f"Desktop switch failed: {e}", file=sys.stderr)


# ---------- Animated background ----------
class AnimatedBackground(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._t = 0.0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(33)

    def _tick(self):
        self._t += 0.012
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        base = QLinearGradient(0, 0, w, h)
        base.setColorAt(0.0, QColor(18, 20, 38))
        base.setColorAt(1.0, QColor(36, 22, 54))
        painter.fillRect(self.rect(), base)

        blobs = [
            (math.sin(self._t) * 0.5 + 0.5,
             math.cos(self._t * 0.7) * 0.5 + 0.5,
             QColor(80, 100, 220, 110)),
            (math.sin(self._t * 0.8 + 2) * 0.5 + 0.5,
             math.cos(self._t * 1.1 + 1) * 0.5 + 0.5,
             QColor(180, 70, 200, 90)),
            (math.sin(self._t * 1.2 + 4) * 0.5 + 0.5,
             math.cos(self._t * 0.5 + 3) * 0.5 + 0.5,
             QColor(60, 180, 200, 80)),
        ]
        for fx, fy, color in blobs:
            cx, cy = fx * w, fy * h
            radius = max(w, h) * 0.55
            rg = QRadialGradient(QPointF(cx, cy), radius)
            rg.setColorAt(0.0, color)
            transparent = QColor(color)
            transparent.setAlpha(0)
            rg.setColorAt(1.0, transparent)
            painter.setBrush(QBrush(rg))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(QPointF(cx, cy), radius, radius)


# ---------- Hotkey recording button ----------
class HotkeyButton(QPushButton):
    hotkey_changed = pyqtSignal(str)

    def __init__(self, current_hotkey, parent=None):
        super().__init__(current_hotkey, parent)
        self.current_hotkey = current_hotkey
        self.recording = False
        self.clicked.connect(self.start_recording)
        self.setStyleSheet(self._style_normal())
        self.setMinimumHeight(44)
        self.setCursor(Qt.PointingHandCursor)

    def _style_normal(self):
        return """
            QPushButton {
                background-color: rgba(255,255,255,0.08);
                color: white;
                border: 1px solid rgba(255,255,255,0.15);
                border-radius: 10px;
                padding: 8px 14px;
                font-family: Consolas, 'Cascadia Code', monospace;
                font-size: 13px;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: rgba(255,255,255,0.14);
                border: 1px solid rgba(255,255,255,0.28);
            }
        """

    def _style_recording(self):
        return """
            QPushButton {
                background-color: rgba(220,60,80,0.5);
                color: white;
                border: 1px solid rgba(255,120,140,0.8);
                border-radius: 10px;
                padding: 8px 14px;
                font-family: Consolas, monospace;
                font-size: 13px;
                font-weight: 600;
            }
        """

    def start_recording(self):
        if self.recording:
            return
        self.recording = True
        self.setText("Press keys...  (Esc to cancel)")
        self.setStyleSheet(self._style_recording())
        threading.Thread(target=self._record, daemon=True).start()

    def _record(self):
        try:
            new_hotkey = keyboard.read_hotkey(suppress=False)
            if new_hotkey.lower() in ("esc", "escape"):
                self.hotkey_changed.emit(self.current_hotkey)
            else:
                self.current_hotkey = new_hotkey
                self.hotkey_changed.emit(new_hotkey)
        except Exception as e:
            print(f"Recording failed: {e}", file=sys.stderr)
            self.hotkey_changed.emit(self.current_hotkey)
        finally:
            self.recording = False
            QTimer.singleShot(0, self._reset_style)

    def _reset_style(self):
        self.setText(self.current_hotkey)
        self.setStyleSheet(self._style_normal())


# ---------- Run/Stop toggle ----------
class RunStopButton(QPushButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._running = True
        self.setMinimumHeight(56)
        self.setCursor(Qt.PointingHandCursor)
        self._apply_style()

    def set_running(self, running):
        self._running = running
        self._apply_style()

    def is_running(self):
        return self._running

    def _apply_style(self):
        if self._running:
            self.setText("⏸  STOP")
            self.setStyleSheet("""
                QPushButton {
                    background-color: rgba(80,224,120,0.18);
                    color: #b8ffd0;
                    border: 1.5px solid rgba(80,224,120,0.55);
                    border-radius: 14px;
                    font-size: 16px;
                    font-weight: 700;
                    letter-spacing: 2px;
                }
                QPushButton:hover {
                    background-color: rgba(80,224,120,0.28);
                    border: 1.5px solid rgba(80,224,120,0.85);
                }
            """)
        else:
            self.setText("▶  RUN")
            self.setStyleSheet("""
                QPushButton {
                    background-color: rgba(220,90,90,0.18);
                    color: #ffc8c8;
                    border: 1.5px solid rgba(220,90,90,0.55);
                    border-radius: 14px;
                    font-size: 16px;
                    font-weight: 700;
                    letter-spacing: 2px;
                }
                QPushButton:hover {
                    background-color: rgba(220,90,90,0.28);
                    border: 1.5px solid rgba(220,90,90,0.85);
                }
            """)


# ---------- Main window ----------
class MainWindow(QWidget):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.setWindowTitle("Desktop Switcher")
        self.resize(480, 560)
        self.setMinimumSize(440, 540)
        self.setMaximumSize(560, 640)

        self.bg = AnimatedBackground(self)
        self.bg.lower()

        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(28, 26, 28, 22)

        # Title
        title = QLabel("Desktop Switcher")
        title.setStyleSheet(
            "font-size: 24px; font-weight: 700; color: white; "
            "letter-spacing: 0.5px; background: transparent;"
        )
        layout.addWidget(title)

        subtitle = QLabel("macOS-style desktop switching for Windows")
        subtitle.setStyleSheet(
            "color: rgba(255,255,255,0.55); font-size: 12px; background: transparent;"
        )
        layout.addWidget(subtitle)

        self.status_label = QLabel("● Running")
        self.status_label.setStyleSheet(
            "color: #6effa0; font-size: 14px; font-weight: 600; "
            "margin-top: 4px; background: transparent;"
        )
        layout.addWidget(self.status_label)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("background-color: rgba(255,255,255,0.08); max-height: 1px;")
        layout.addWidget(line)

        hk_title = QLabel("HOTKEYS")
        hk_title.setStyleSheet(
            "color: rgba(255,255,255,0.5); font-size: 11px; "
            "font-weight: 700; letter-spacing: 2px; background: transparent;"
        )
        layout.addWidget(hk_title)

        # Next desktop
        row_r = QHBoxLayout()
        lbl_r = QLabel("Next Desktop  →")
        lbl_r.setMinimumWidth(170)
        lbl_r.setStyleSheet(
            "font-size: 13px; color: rgba(255,255,255,0.85); background: transparent;"
        )
        self.btn_right = HotkeyButton(self.controller.config["hotkey_right"])
        self.btn_right.hotkey_changed.connect(
            lambda hk: self.controller.update_hotkey("right", hk)
        )
        row_r.addWidget(lbl_r)
        row_r.addWidget(self.btn_right, 1)
        layout.addLayout(row_r)

        # Previous desktop
        row_l = QHBoxLayout()
        lbl_l = QLabel("←  Previous Desktop")
        lbl_l.setMinimumWidth(170)
        lbl_l.setStyleSheet(
            "font-size: 13px; color: rgba(255,255,255,0.85); background: transparent;"
        )
        self.btn_left = HotkeyButton(self.controller.config["hotkey_left"])
        self.btn_left.hotkey_changed.connect(
            lambda hk: self.controller.update_hotkey("left", hk)
        )
        row_l.addWidget(lbl_l)
        row_l.addWidget(self.btn_left, 1)
        layout.addLayout(row_l)

        # Warning for risky hotkeys
        self.warn_label = QLabel("")
        self.warn_label.setStyleSheet(
            "color: #ffb060; font-size: 11px; background: transparent;"
        )
        self.warn_label.setWordWrap(True)
        layout.addWidget(self.warn_label)
        self._update_warning()

        # Divider
        line2 = QFrame()
        line2.setFrameShape(QFrame.HLine)
        line2.setStyleSheet(
            "background-color: rgba(255,255,255,0.08); max-height: 1px; margin-top: 6px;"
        )
        layout.addWidget(line2)

        # Options
        opt_title = QLabel("OPTIONS")
        opt_title.setStyleSheet(
            "color: rgba(255,255,255,0.5); font-size: 11px; "
            "font-weight: 700; letter-spacing: 2px; background: transparent;"
        )
        layout.addWidget(opt_title)

        self.autostart_cb = QCheckBox("Start with Windows (in background)")
        self.autostart_cb.setChecked(autostart_is_enabled())
        self.autostart_cb.setCursor(Qt.PointingHandCursor)
        self.autostart_cb.setStyleSheet("""
            QCheckBox {
                color: rgba(255,255,255,0.9);
                font-size: 13px;
                background: transparent;
                spacing: 10px;
            }
            QCheckBox::indicator {
                width: 18px; height: 18px;
                border-radius: 5px;
                border: 1.5px solid rgba(255,255,255,0.3);
                background-color: rgba(255,255,255,0.05);
            }
            QCheckBox::indicator:hover {
                border: 1.5px solid rgba(255,255,255,0.55);
            }
            QCheckBox::indicator:checked {
                background-color: #6effa0;
                border: 1.5px solid #6effa0;
                image: none;
            }
        """)
        self.autostart_cb.toggled.connect(self._on_autostart_toggled)
        layout.addWidget(self.autostart_cb)

        autostart_hint = QLabel("Launches into the tray on login — no window opens.")
        autostart_hint.setStyleSheet(
            "color: rgba(255,255,255,0.4); font-size: 10px; background: transparent; "
            "margin-left: 28px;"
        )
        autostart_hint.setWordWrap(True)
        layout.addWidget(autostart_hint)

        layout.addStretch()

        self.toggle_btn = RunStopButton()
        self.toggle_btn.set_running(self.controller.config.get("enabled", True))
        self.toggle_btn.clicked.connect(self._on_toggle)
        layout.addWidget(self.toggle_btn)

        hint = QLabel("Close window → keeps running in the tray")
        hint.setStyleSheet(
            "color: rgba(255,255,255,0.35); font-size: 10px; background: transparent;"
        )
        hint.setAlignment(Qt.AlignCenter)
        layout.addWidget(hint)

    def resizeEvent(self, event):
        self.bg.setGeometry(0, 0, self.width(), self.height())
        super().resizeEvent(event)

    def _update_warning(self):
        modifiers = ("ctrl", "shift", "alt", "windows", "cmd")
        hk_r = self.controller.config["hotkey_right"].lower()
        hk_l = self.controller.config["hotkey_left"].lower()
        risky = []
        if not any(m in hk_r for m in modifiers):
            risky.append(hk_r)
        if not any(m in hk_l for m in modifiers):
            risky.append(hk_l)
        if risky:
            self.warn_label.setText(
                f"⚠ Hotkey without modifier ({', '.join(risky)}) — "
                "will block normal keyboard input! Use Shift/Ctrl/Alt + key."
            )
        else:
            self.warn_label.setText("")

    def _on_autostart_toggled(self, checked):
        if checked:
            ok = autostart_enable()
            if not ok:
                QMessageBox.warning(self, "Autostart",
                    "Could not enable autostart.")
                self.autostart_cb.blockSignals(True)
                self.autostart_cb.setChecked(False)
                self.autostart_cb.blockSignals(False)
        else:
            autostart_disable()

    def _on_toggle(self):
        new_state = not self.toggle_btn.is_running()
        self.toggle_btn.set_running(new_state)
        self.controller.set_enabled(new_state)
        if new_state:
            self.status_label.setText("● Running")
            self.status_label.setStyleSheet(
                "color: #6effa0; font-size: 14px; font-weight: 600; "
                "margin-top: 4px; background: transparent;"
            )
        else:
            self.status_label.setText("● Stopped")
            self.status_label.setStyleSheet(
                "color: #ff8080; font-size: 14px; font-weight: 600; "
                "margin-top: 4px; background: transparent;"
            )

    def closeEvent(self, event):
        event.ignore()
        self.hide()
        if self.controller.tray:
            self.controller.tray.showMessage(
                "Desktop Switcher",
                "Still running in the background. Right-click the tray icon to quit.",
                QSystemTrayIcon.Information, 2500
            )


# ---------- Controller ----------
class Controller(QObject):
    def __init__(self, qt_app):
        super().__init__()
        self.qt_app = qt_app
        self.config = load_config()
        self.overlay = SwipeOverlay()
        self.bridge = HotkeyBridge()
        self.bridge.triggered.connect(self.on_hotkey)
        self._lock = False
        self.tray = None
        self.window = None

        if self.config.get("enabled", True):
            self._register_hotkeys()

    def _register_hotkeys(self):
        try:
            keyboard.unhook_all_hotkeys()
        except Exception:
            pass
        try:
            keyboard.add_hotkey(
                self.config["hotkey_right"],
                lambda: self.bridge.triggered.emit("right"),
                suppress=True,
            )
            keyboard.add_hotkey(
                self.config["hotkey_left"],
                lambda: self.bridge.triggered.emit("left"),
                suppress=True,
            )
        except Exception as e:
            print(f"Hotkey registration failed: {e}", file=sys.stderr)

    def set_enabled(self, enabled):
        self.config["enabled"] = enabled
        save_config(self.config)
        if enabled:
            self._register_hotkeys()
        else:
            try:
                keyboard.unhook_all_hotkeys()
            except Exception:
                pass

    def update_hotkey(self, direction, new_hotkey):
        key = "hotkey_right" if direction == "right" else "hotkey_left"
        other_key = "hotkey_left" if direction == "right" else "hotkey_right"
        if new_hotkey == self.config[other_key]:
            QMessageBox.warning(
                self.window, "Hotkey conflict",
                f"'{new_hotkey}' is already assigned to the other direction."
            )
            btn = self.window.btn_right if direction == "right" else self.window.btn_left
            btn.current_hotkey = self.config[key]
            btn.setText(self.config[key])
            return
        self.config[key] = new_hotkey
        save_config(self.config)
        if self.config.get("enabled", True):
            self._register_hotkeys()
        if self.window:
            self.window._update_warning()

    def on_hotkey(self, direction):
        if self._lock or not self.config.get("enabled", True):
            return
        self._lock = True
        QTimer.singleShot(500, self._unlock)
        self.overlay.play(direction)
        QTimer.singleShot(80, lambda: switch_desktop(direction))

    def _unlock(self):
        self._lock = False


# ---------- Tray icon ----------
def make_tray_icon():
    pix = QPixmap(64, 64)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor(110, 255, 160))
    painter.setPen(Qt.NoPen)
    painter.drawRoundedRect(8, 8, 48, 48, 14, 14)
    painter.end()
    return QIcon(pix)


def setup_tray(controller):
    tray = QSystemTrayIcon(make_tray_icon())
    tray.setToolTip("Desktop Switcher")
    menu = QMenu()

    show_action = QAction("Show window")
    show_action.triggered.connect(
        lambda: (controller.window.show(),
                 controller.window.raise_(),
                 controller.window.activateWindow())
    )
    menu.addAction(show_action)
    menu.addSeparator()

    quit_action = QAction("Quit")
    quit_action.triggered.connect(QApplication.instance().quit)
    menu.addAction(quit_action)

    tray.setContextMenu(menu)
    tray.activated.connect(lambda reason: (
        controller.window.show(),
        controller.window.raise_(),
        controller.window.activateWindow()
    ) if reason == QSystemTrayIcon.DoubleClick else None)
    tray.show()
    return tray


# ---------- Entry point ----------
def main():
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    qt_app = QApplication(sys.argv)
    qt_app.setQuitOnLastWindowClosed(False)

    controller = Controller(qt_app)
    window = MainWindow(controller)
    controller.window = window
    controller.tray = setup_tray(controller)

    started_via_autostart = AUTOSTART_FLAG in sys.argv
    if not started_via_autostart:
        window.show()

    sys.exit(qt_app.exec_())


if __name__ == "__main__":
    main()