"""Fresh connected viewer for the export/stdio lifecycle regression test."""
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PySide6 import QtCore, QtTest, QtWidgets
from transport_history_viewer import CommandBridge, HistoryViewerWindow, report


root, selection = Path(sys.argv[1]), sys.argv[2]
QtCore.QSettings.setPath(QtCore.QSettings.Format.IniFormat, QtCore.QSettings.Scope.UserScope, str(root))
folder = root / 'map_2d'
folder.mkdir()
source = folder / 'sample.csv'
source.write_text('Doping,Vds,Ids_DC,PassIndex,FastDirection\nV,V,A,#,\n'
                  '-1,0,1e-12,0,forward\n1,0,2e-12,0,forward\n'
                  '-1,1,3e-12,1,forward\n1,1,4e-12,1,forward\n', encoding='utf-8')
source.with_name('sample_metadata.json').write_text(json.dumps({
    'measurement': 'map_2d', 'created_at': '2026-10-04T12:00:00',
    'params': {'axis_fast': 'Doping', 'axis_slow': 'Vds'}}), encoding='utf-8')


class ObservedViewer(HistoryViewerWindow):
    def handle_command(self, message):
        report('command_received', message=message)
        super().handle_command(message)


app = QtWidgets.QApplication([])
window = ObservedViewer(folder, connected=True)
bridge = CommandBridge()
bridge.received.connect(window.handle_command)
threading.Thread(target=bridge.read_commands, daemon=True).start()
window.show()
stage = 0


def tick():
    global stage
    page = window.page
    if stage == 0 and page.model.records and not page.busy:
        stage = 1
        page.measurement_tabs.setCurrentIndex(2)
        page.model.setData(page.model.index(0, 0), QtCore.Qt.CheckState.Checked, QtCore.Qt.ItemDataRole.CheckStateRole)
    elif stage == 1 and page.png_actions._is_ready():
        stage = 2
        report('export_started', spool=str(page.png_actions.exporter.spool))
        def select_item():
            menu = page.png_actions.menu
            action = getattr(page.png_actions, selection + '_action')
            QtTest.QTest.mouseClick(menu, QtCore.Qt.MouseButton.LeftButton,
                                   pos=menu.actionGeometry(action).center())
        QtCore.QTimer.singleShot(50, select_item)
        QtTest.QTest.mouseClick(page.export_button, QtCore.Qt.MouseButton.LeftButton)
    elif stage == 2 and page.last_export_result:
        stage = 3
        report('export_complete', visible=window.isVisible(), result=page.last_export_result)


timer = QtCore.QTimer()
timer.setInterval(50)
timer.timeout.connect(tick)
timer.start()
report('ready')
sys.exit(app.exec())
