from PySide6 import QtCore, QtWidgets

from app.settings import get_app_settings
from app.ui.widgets.safe_combo import SafeComboBox
from app.ui.widgets.safe_spinbox import SafeDoubleSpinBox


# Each display unit represents the same 0.001 Ω resolution and physical range.
RS_UNITS = (('Ω', 1., 3), ('kΩ', 1000., 6), ('MΩ', 1000000., 9))


class _ResistanceSpinBox(SafeDoubleSpinBox):
    def textFromValue(self, value):
        # Keep Qt's locale-aware formatting/parser pair, trimming only decimal
        # trailing zeros so 100 kΩ and 0.1 MΩ remain easy to read.
        text = super().textFromValue(value)
        separator = self.locale().decimalPoint()
        if separator in text:
            text = text.rstrip(self.locale().zeroDigit()).removesuffix(separator)
        return text


class DragDriveSettings(QtWidgets.QGroupBox):
    changed = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__('Measurement mode', parent)
        form = QtWidgets.QFormLayout(self)
        form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
        self.cbo_mode = SafeComboBox()
        self.cbo_mode.addItems(['Ordinary (one lock-in)', 'Drag / Drive (two lock-ins)'])
        self.sp_rs = _ResistanceSpinBox()
        self.sp_rs.setAccessibleName('Drive series resistance')
        self.cbo_rs_unit = SafeComboBox()
        self.cbo_rs_unit.setAccessibleName('Drive series resistance unit')
        self.cbo_rs_unit.setToolTip('Changing units converts the value without changing the resistance.')
        for label, factor, _decimals in RS_UNITS:
            self.cbo_rs_unit.addItem(label, factor)
        rs_input = QtWidgets.QWidget()
        rs_input.setFocusProxy(self.sp_rs)
        rs_layout = QtWidgets.QHBoxLayout(rs_input)
        rs_layout.setContentsMargins(0, 0, 0, 0)
        rs_layout.addWidget(self.sp_rs, 1)
        rs_layout.addWidget(self.cbo_rs_unit)
        rs_label = QtWidgets.QLabel('Drive &Rs:')
        rs_label.setBuddy(self.sp_rs)
        form.addRow('Mode:', self.cbo_mode)
        form.addRow(rs_label, rs_input)
        QtWidgets.QWidget.setTabOrder(self.cbo_mode, self.sp_rs)
        QtWidgets.QWidget.setTabOrder(self.sp_rs, self.cbo_rs_unit)
        self.lbl_hint = QtWidgets.QLabel()
        self.lbl_hint.setWordWrap(True)
        form.addRow(self.lbl_hint)
        settings = get_app_settings()
        self.cbo_mode.setCurrentIndex(1 if settings.value('drag_drive/enabled', False, type=bool) else 0)
        unit_index = self.cbo_rs_unit.findText(settings.value('drag_drive/rs_unit', 'kΩ', type=str))
        self.cbo_rs_unit.setCurrentIndex(unit_index if unit_index >= 0 else 1)
        self._set_resistance_display(float(settings.value('drag_drive/rs_ohm', 100000.)))
        self.cbo_mode.currentIndexChanged.connect(self._edited)
        self.sp_rs.valueChanged.connect(self._edited)
        self.cbo_rs_unit.currentIndexChanged.connect(self._unit_changed)
        self._manager = None
        self._update_enabled()

    def configuration(self):
        return {'enabled': self.cbo_mode.currentIndex() == 1,
                'series_resistance_ohm': self.sp_rs.value() * self._rs_factor,
                'drive_preamp': 'SR551', 'drive_preamp_gain': 10.,
                'preamp_source': 'user configuration', 'verified': False}

    def _set_resistance_display(self, ohms):
        _label, factor, decimals = RS_UNITS[self.cbo_rs_unit.currentIndex()]
        self._rs_factor = factor
        # Changing decimals/range can clamp the old display value. Publish only
        # the final converted value, never those intermediate configurations.
        with QtCore.QSignalBlocker(self.sp_rs):
            self.sp_rs.setDecimals(decimals)
            self.sp_rs.setRange(.001 / factor, 1e12 / factor)
            self.sp_rs.setValue(ohms / factor)

    def _unit_changed(self, *_):
        ohms = self.configuration()['series_resistance_ohm']
        self._set_resistance_display(ohms)
        self._edited()

    def bind_manager(self, manager):
        if self._manager is manager:
            return
        self._manager = manager
        manager.resources_changed.connect(self._update_enabled)
        manager.operation_changed.connect(self._update_enabled)
        self._update_enabled()

    def _edited(self, *_):
        settings = get_app_settings()
        config = self.configuration()
        settings.setValue('drag_drive/enabled', config['enabled'])
        settings.setValue('drag_drive/rs_ohm', config['series_resistance_ohm'])
        settings.setValue('drag_drive/rs_unit', self.cbo_rs_unit.currentText())
        settings.sync()
        self._update_enabled()
        self.changed.emit()

    def _update_enabled(self, *_):
        available = self._manager is None or (not self._manager.current_in_use() and not self._manager.is_busy())
        dual = self.configuration()['enabled']
        self.cbo_mode.setEnabled(available)
        self.sp_rs.setEnabled(available and dual)
        self.cbo_rs_unit.setEnabled(available and dual)
        self.lbl_hint.setText(
            'Drag: AI0/AI1 + SR570. DC: AI2. Drive: AI3/AI4 + SR551 x10 (user configured). '
            'Use Connect All after changing mode. Rs and both sensitivities are frozen per run.'
            if dual else 'Ordinary X/Y/DC acquisition. The second lock-in is not required or used for conversion.')
