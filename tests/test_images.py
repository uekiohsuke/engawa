from fastapi.testclient import TestClient
from PySide6.QtCore import QObject, QPoint, QPointF, QSettings, Qt, Signal
from PySide6.QtGui import QWheelEvent

from engawa.characters import ASSET_DIR, CHARACTERS
from engawa.config import Settings
from engawa.core.app import create_app
from engawa.core.llm import MockEmbedder, MockLLMClient
from engawa.ui.dialogue_window import PANEL_WIDTH, STANDING_HEIGHT, STANDING_HEIGHT_STEP, DialogueWindow
from engawa.ui.images import icon_pixmap, standing_path, standing_pixmap
from engawa.ui.widgets import MessageView
from test_memory import FakeClock, at

SUI = {
    "id": "sui",
    "name": "翠",
    "icon": "icon/sui_chibi_normal.png",
    "standing": {"normal": "standing/sui_standing_normal.png"},
}


def test_characters_api_returns_image_paths(tmp_path):
    settings = Settings(data_dir=tmp_path, judge_interval_seconds=0)
    app = create_app(settings, llm=MockLLMClient(delay=0), clock=FakeClock(at(12)), embedder=MockEmbedder())
    with TestClient(app) as client:
        [sui] = client.get("/characters").json()
    assert sui == SUI


def test_every_character_image_exists():
    for character in CHARACTERS.values():
        paths = [character.images.icon, *character.images.standing.values()]
        for path in filter(None, paths):
            assert (ASSET_DIR / path).is_file(), path


def test_icon_is_cropped_to_a_circle(qt_app):
    pixmap = icon_pixmap(SUI["icon"], 40, "翠")
    image = pixmap.toImage()
    assert pixmap.deviceIndependentSize().toSize().width() == 40
    assert image.pixelColor(0, 0).alpha() == 0  # 角は透明
    assert image.pixelColor(image.width() // 2, image.height() // 2).alpha() == 255


def test_icon_falls_back_to_initial_without_image(qt_app):
    for relative in (None, "icon/存在しない.png"):
        image = icon_pixmap(relative, 32, "翠", "#ff0000").toImage()
        assert image.pixelColor(image.width() // 2, 2).red() == 255


def test_standing_expression_falls_back_to_default():
    assert standing_path(SUI, "happy") == "standing/sui_standing_normal.png"
    assert standing_path({"standing": {}}) is None
    assert standing_path({"standing": None}) is None


def test_standing_is_scaled_to_height(qt_app):
    pixmap = standing_pixmap(SUI["standing"]["normal"], 400)
    assert pixmap.deviceIndependentSize().toSize().height() == 400
    assert standing_pixmap("standing/存在しない.png", 400) is None


# --- 表示 ---


class FakeClient(QObject):
    event_received = Signal(dict)

    def get(self, path, on_success=None, on_error=None):
        pass

    def post(self, path, body, on_success=None, on_error=None):
        pass


def test_dialogue_window_stands_on_the_screen(qt_app):
    QSettings().clear()
    window = DialogueWindow(FakeClient(), SUI, session_id=1)
    assert window.height() == STANDING_HEIGHT
    assert window.width() > PANEL_WIDTH
    window.show()
    bottom_right = window.frameGeometry().bottomRight()

    # Ctrl+ホイールで大きくしても、足元（右下の角）は動かない
    event = QWheelEvent(
        QPointF(10, 10), QPointF(10, 10), QPoint(), QPoint(0, 120),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.ControlModifier, Qt.ScrollPhase.NoScrollPhase, False,
    )
    window.wheelEvent(event)
    assert window.height() == STANDING_HEIGHT + STANDING_HEIGHT_STEP
    assert window.frameGeometry().bottomRight() == bottom_right
    assert QSettings().value("dialogue/standing_height", type=int) == STANDING_HEIGHT + STANDING_HEIGHT_STEP

    window.set_expression("happy")  # 未登録の表情は既定の立ち絵のまま
    assert window.height() == STANDING_HEIGHT + STANDING_HEIGHT_STEP
    window.close()
    QSettings().clear()


def test_dialogue_window_without_standing_shows_panel_only(qt_app):
    window = DialogueWindow(FakeClient(), {"id": "x", "name": "テスト", "icon": None, "standing": {}}, session_id=1)
    assert (window.width(), window.height()) == (PANEL_WIDTH, 220)


def test_message_view_shows_avatars(qt_app):
    view = MessageView()
    view.set_character(SUI)
    view.add_message({"role": "character", "content": "やあ", "created_at": None})
    view.add_message({"role": "user", "content": "こんにちは", "created_at": None})
    view.add_message({"role": "system", "content": "（通知）", "created_at": None})
    rows = [view.widget().layout().itemAt(i).widget() for i in range(1, 4)]
    avatars = [row.layout().itemAt(0).widget().pixmap() for row in rows]
    assert [not a.isNull() for a in avatars] == [True, True, False]
