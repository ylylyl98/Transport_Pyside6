"""Application QSS source; APP_STYLE remains available to existing callers."""
from pathlib import Path

STYLE_PATH = Path(__file__).with_name("app.qss")


def read_app_style(path=None, *, font_size_pt=10) -> str:
    size = int(font_size_pt)
    if isinstance(font_size_pt, bool) or size != float(font_size_pt) or not 7 <= size <= 18:
        raise ValueError("UI font size must be a whole number from 7 to 18 pt")
    return Path(path or STYLE_PATH).read_text(encoding="utf-8").replace("@BASE_FONT_PT@", str(size))


try:
    APP_STYLE = read_app_style()
except (OSError, UnicodeError):
    # Let the reload action report a missing or unreadable external sheet.
    APP_STYLE = ""
