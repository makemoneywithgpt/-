import sys
from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from mate.widget import DesktopMate


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("쿠리만쥬 업무메이트")
    settings = QSettings("DesktopMate", "Kurimanju")
    app_dir = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
    widget = DesktopMate(settings, app_dir / "assets")
    widget.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
