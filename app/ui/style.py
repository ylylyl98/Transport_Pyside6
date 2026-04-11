APP_STYLE = """
QMainWindow, QWidget {
    background: #F4F5F7;
    color: #212121;
    font-family: "Segoe UI", Arial, sans-serif;
    font-size: 10pt;
}
QToolBar {
    background: #F4F5F7;
    border: none;
    spacing: 6px;
    padding: 6px;
}
QToolBar QLabel {
    color: #374151;
}
QGroupBox {
    background: #FFFFFF;
    border: 1px solid #E1E4E8;
    border-radius: 8px;
    margin-top: 12px;
    padding-top: 10px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: #374151;
    font-weight: 600;
}
QScrollArea, QPlainTextEdit {
    background: #FFFFFF;
}
QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox {
    background: #FFFFFF;
    border: 1px solid #CDD3DA;
    border-radius: 5px;
    padding: 2px 6px;
    selection-background-color: #DBEAFE;
}
QLineEdit:hover, QComboBox:hover, QDoubleSpinBox:hover, QSpinBox:hover {
    border-color: #A8B0BB;
}
QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus, QSpinBox:focus {
    border: 2px solid #2563EB;
    padding: 1px 5px;
}
QLineEdit:disabled, QComboBox:disabled, QDoubleSpinBox:disabled, QSpinBox:disabled {
    color: #9CA3AF;
    background: #F9FAFB;
    border-color: #E5E7EB;
}
QLineEdit[reconnect="true"], QComboBox[reconnect="true"], QDoubleSpinBox[reconnect="true"], QSpinBox[reconnect="true"] {
    border: 2px solid #D97706;
    background: #FFF7ED;
    padding: 1px 5px;
}
QComboBox::drop-down, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button, QSpinBox::up-button, QSpinBox::down-button {
    subcontrol-origin: padding;
    width: 20px;
    background: #F9FAFB;
    border-left: 1px solid #CDD3DA;
    border-top-right-radius: 5px;
    border-bottom-right-radius: 5px;
}
QDoubleSpinBox::up-button, QSpinBox::up-button {
    border-bottom: 1px solid #CDD3DA;
    border-bottom-right-radius: 0px;
}
QDoubleSpinBox::down-button, QSpinBox::down-button {
    border-top-right-radius: 0px;
}
QComboBox::down-arrow, QDoubleSpinBox::up-arrow, QDoubleSpinBox::down-arrow, QSpinBox::up-arrow, QSpinBox::down-arrow {
    width: 8px;
    height: 8px;
}
QPlainTextEdit {
    color: #212121;
    font-family: Consolas, "Courier New", monospace;
    font-size: 9pt;
    border: 1px solid #E1E4E8;
    border-radius: 6px;
}
QPushButton {
    padding: 5px 12px;
    border: 1px solid #D0D0D0;
    border-radius: 6px;
    background: #FAFAFA;
}
QPushButton:hover {
    border-color: #A8B0BB;
    background: #F3F4F6;
}
QPushButton[role="primary"] {
    background: #1D4ED8;
    color: white;
    font-size: 13px;
    font-weight: bold;
    border-color: #1D4ED8;
}
QPushButton[role="primary"]:hover {
    background: #2563EB;
    border-color: #2563EB;
}
QPushButton[role="danger"] {
    background: #B91C1C;
    color: white;
    font-weight: bold;
    border-color: #B91C1C;
}
QPushButton[role="danger"]:hover {
    background: #991B1B;
    border-color: #991B1B;
}
QPushButton[role="dev"] {
    color: #666666;
}
QPushButton[flash="ok"] {
    background: #C8E6C9;
    border-color: #A5D6A7;
}
QPushButton:disabled {
    color: #9CA3AF;
    background-color: #F3F4F6;
    border-color: #E5E7EB;
}
QToolButton[role="expander-header"] {
    background: transparent;
    border: none;
    color: #374151;
    font-weight: 600;
    padding: 4px 2px;
    text-align: left;
}
QToolButton[role="expander-header"]:hover {
    color: #1F2937;
    background: #F8FAFC;
    border-radius: 6px;
}
QLabel[role="section-header"] {
    color: #374151;
    font-size: 9pt;
    font-weight: 700;
    letter-spacing: 0.8px;
    padding: 14px 0 4px 0;
}
QLabel[role="hint"] {
    color: #6B7280;
    font-size: 9pt;
}
QLabel[role="warning-hint"] {
    color: #B45309;
    font-size: 9pt;
    font-weight: 600;
}
QLabel[role="run-status"] {
    font-size: 12px;
    font-weight: bold;
    border-radius: 6px;
    padding: 6px 8px;
    background: transparent;
    color: #616161;
}
QLabel[role="run-status"][state="idle"] {
    background: #EEEEEE;
    color: #616161;
}
QLabel[role="run-status"][state="running"] {
    background: #1D4ED8;
    color: white;
}
QLabel[role="run-status"][state="done"] {
    background: #2E7D32;
    color: white;
}
QLabel[role="run-status"][state="error"] {
    background: #FDECEC;
    color: #9F1239;
    border: 1px solid #F5C2C7;
}
QWidget[role="status-item"] {
    background: #F8FAFC;
    border: 1px solid #E5E7EB;
    border-radius: 8px;
}
QLabel[role="status-name"] {
    color: #374151;
    font-size: 9pt;
    font-weight: 600;
}
QLabel[role="status-pill"] {
    font-size: 10px;
    font-weight: bold;
    border-radius: 9px;
    padding: 3px 8px;
    background: #F3F4F6;
    color: #4B5563;
    border: 1px solid #E5E7EB;
}
QLabel[role="status-pill"][status="ok"] {
    background: #E8F5EE;
    color: #166534;
    border-color: #CDE9D7;
}
QLabel[role="status-pill"][status="err"] {
    background: #FDECEC;
    color: #9F1239;
    border-color: #F5C2C7;
}
QLabel[role="status-pill"][status="idle"] {
    background: #F3F4F6;
    color: #6B7280;
    border-color: #E5E7EB;
}
QLabel[role="status-pill"][status="busy"] {
    background: #E8F0FE;
    color: #1D4ED8;
    border-color: #C7D7FE;
}
QLabel[role="status-pill"][status="warn"] {
    background: #FEF3C7;
    color: #92400E;
    border-color: #F5D28D;
}
QToolButton[role="status-detail"] {
    border: none;
    background: transparent;
    color: #2563EB;
    padding: 2px 4px;
    font-size: 9pt;
}
QToolButton[role="status-detail"]:hover {
    color: #1D4ED8;
    background: #EFF6FF;
    border-radius: 5px;
}
QProgressBar {
    border: 1px solid #DADADA;
    border-radius: 5px;
    text-align: center;
    min-height: 24px;
    background: #FFFFFF;
}
QProgressBar::chunk {
    background: #1D4ED8;
    border-radius: 4px;
}
QTabWidget::pane {
    border: 1px solid #E1E4E8;
    background: #FFFFFF;
    border-radius: 8px;
    top: -1px;
}
QTabBar::tab {
    background: transparent;
    color: #4B5563;
    padding: 10px 14px 8px 14px;
    margin-right: 4px;
    border: none;
    border-bottom: 2px solid transparent;
    font-weight: 500;
}
QTabBar::tab:selected {
    color: #1F2937;
    border-bottom: 2px solid #2563EB;
}
QTabBar::tab:hover:!selected {
    color: #1F2937;
    background: #F8FAFC;
}
QDockWidget {
    titlebar-close-icon: none;
    titlebar-normal-icon: none;
}
QDockWidget::title {
    background: #F4F5F7;
    color: #374151;
    font-weight: 600;
    padding: 6px 10px;
    border: 1px solid #E1E4E8;
    border-bottom: none;
    text-align: left;
}
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 4px 0 4px 0;
}
QScrollBar::handle:vertical {
    background: #CBD5E1;
    min-height: 30px;
    border-radius: 5px;
}
QScrollBar::handle:vertical:hover {
    background: #94A3B8;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical,
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
    background: transparent;
    border: none;
}
QScrollBar:horizontal {
    background: transparent;
    height: 10px;
    margin: 0 4px 0 4px;
}
QScrollBar::handle:horizontal {
    background: #CBD5E1;
    min-width: 30px;
    border-radius: 5px;
}
QScrollBar::handle:horizontal:hover {
    background: #94A3B8;
}
QSplitter::handle {
    background: #E5E7EB;
}
QSplitter::handle:horizontal {
    width: 4px;
}
QSplitter::handle:vertical {
    height: 4px;
}
QFrame[role="separator"] {
    background: #E1E4E8;
    max-height: 1px;
}
"""
