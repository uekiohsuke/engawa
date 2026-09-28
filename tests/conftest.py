import os
import sys

import pytest

# UI のテストは画面を出さずに動かす（QApplication を作る前に指定する必要がある）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qt_app(tmp_path_factory):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    # QSettings は実際の設定（レジストリ）ではなく、一時ディレクトリの ini に書く
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path_factory.mktemp("qsettings")))
    app = QApplication.instance() or QApplication(sys.argv)
    app.setOrganizationName("engawa-test")
    return app
