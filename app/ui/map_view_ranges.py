"""Compact heatmap range editor and in-place viewport color scaling."""
import math

from PySide6 import QtCore, QtWidgets

from app.map_view import visible_clim


class MapViewRanges:
    def __init__(self, page):
        self.page = page
        self.options = {'map_x_auto': True, 'map_y_auto': True, 'clim_auto': True}
        self.axes_names = None
        self.color_signal = None
        self.connections = []
        self.updating = False
        self.last_clim = None
        self.rows, self.mode_controls, self.bound_edits, self.unit_labels = {}, {}, {}, {}
        self._drafts = set()
        self._errors = {}
        self._ranges_visible = False
        self._pending_focus = None
        self.error_label = QtWidgets.QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        self.modes = {'map_xlim': 'map_x_auto', 'map_ylim': 'map_y_auto', 'clim': 'clim_auto'}
        for key, auto in self.modes.items():
            row = QtWidgets.QWidget()
            layout = QtWidgets.QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            mode = QtWidgets.QComboBox()
            mode.addItems(['Auto', 'Fixed'])
            layout.addWidget(mode)
            edits = []
            for title in (('vmin', 'vmax') if key == 'clim' else ('min', 'max')):
                edit = QtWidgets.QLineEdit()
                edit.setReadOnly(True)
                edit.setMinimumWidth(100)
                edit.setMaximumWidth(134)
                edit.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Fixed)
                label = QtWidgets.QLabel(title)
                label.setBuddy(edit)
                layout.addWidget(label)
                layout.addWidget(edit, 1)
                edit.textEdited.connect(lambda _text, key=key: self._drafts.add(key))
                edit.editingFinished.connect(lambda key=key: self._commit_range(key))
                edits.append(edit)
            unit = QtWidgets.QLabel()
            layout.addWidget(unit)
            self.rows[key], self.mode_controls[key] = row, mode
            self.bound_edits[key], self.unit_labels[key] = edits, unit
            mode.currentIndexChanged.connect(lambda _index, key=key: self._mode_changed(key))
            row.setEnabled(False)

    def state(self):
        # The selected signal can change before its asynchronous map is drawn.
        # Persist the owner of these bounds, not the pending combo selection.
        return {**self.options, 'clim_signal': self.color_signal}

    def restore(self, view):
        self.options = {key: view.get(key, default) for key, default in
                        (('map_x_auto', 'map_xlim' not in view), ('map_y_auto', 'map_ylim' not in view),
                         ('clim_auto', 'clim' not in view))}
        for key in ('map_xlim', 'map_ylim', 'clim'):
            if view.get(key) is not None:
                self.options[key] = list(view[key])
        self.axes_names = (view.get('x'), view.get('map_y')) if view.get('x') else None
        self.color_signal = view.get('clim_signal', view.get('signal'))
        self._drafts.clear()
        self._errors.clear()
        self._sync_errors()
        self._sync_fields()

    def detach(self):
        for callbacks, number in self.connections:
            callbacks.disconnect(number)
        self.connections.clear()
        focus = self.page.focusWidget()
        if any(focus in edits for edits in self.bound_edits.values()):
            self._pending_focus = focus
            # Only restore focus within this redraw, never on a later selection.
            QtCore.QTimer.singleShot(0, self._forget_focus)
        previous_updating, self.updating = self.updating, True
        try:
            # Disabling a focused editor emits editingFinished. A live redraw
            # must not turn that temporary focus loss into a user commit.
            for row in self.rows.values():
                row.setEnabled(False)
        finally:
            self.updating = previous_updating

    def _forget_focus(self):
        self._pending_focus = None

    def attach(self, primary):
        self.detach()
        names = (primary.x_name, primary.y_name)
        if self.axes_names is not None and self.axes_names != names:
            self.options.update(map_x_auto=True, map_y_auto=True)
            self._drafts.difference_update(('map_xlim', 'map_ylim'))
            for key in ('map_xlim', 'map_ylim'):
                self._errors.pop(key, None)
        self.axes_names = names
        if self.color_signal is not None and self.color_signal != primary.signal:
            # Fixed bounds belong to the displayed signal. Drag and drive may
            # differ by orders of magnitude even when both use amperes.
            self.options['clim_auto'] = True
            self._drafts.discard('clim')
            self._errors.pop('clim', None)
        self.color_signal = primary.signal
        self.apply(self.options)
        ax, mesh = self.page.map_ax, self.page._colorbar.mappable
        for event in ('xlim_changed', 'ylim_changed'):
            self.connections.append((ax.callbacks, ax.callbacks.connect(event, self._axes_changed)))
        self.connections.append((mesh.callbacks, mesh.callbacks.connect('changed', self._image_changed)))
        for row in self.rows.values():
            row.setEnabled(True)
        self._sync_fields()
        self._sync_errors()
        if self._pending_focus is not None and self._pending_focus.isVisible():
            self._pending_focus.setFocus()
        self._pending_focus = None

    def _set_clim(self, limits):
        mesh = self.page._colorbar.mappable
        # Publish both bounds together: colorbar callbacks must not normalize
        # an intermediate inverted interval when switching from e.g. 0..5 to 11..14.
        with mesh.norm.callbacks.blocked():
            mesh.set_clim(*limits)
        self.last_clim = mesh.get_clim()
        self.options['clim'] = list(self.last_clim)
        mesh.changed()

    def _auto_color(self):
        primary = self.page._displayed_primary
        if primary is None or not self.options['clim_auto']:
            return
        limits = visible_clim({'x': primary.x, 'y': primary.y, 'z': primary.z},
                              self.page.map_ax.get_xlim(), self.page.map_ax.get_ylim())
        if limits is not None:
            self._set_clim(limits)
            self.rows['clim'].setToolTip('Auto color follows finite measured values within the visible X/Y range. CSV keeps the full data.')
        else:
            self._set_clim(self.options.get('clim', self.page._colorbar.mappable.get_clim()))
            self.rows['clim'].setToolTip('No finite measured points in this X/Y range; keeping the previous color bounds. CSV keeps the full data.')

    def _axes_changed(self, ax):
        if self.updating or self.page._displayed_primary is None:
            return
        self.updating = True
        try:
            self.options.update(map_x_auto=ax.get_autoscalex_on(), map_y_auto=ax.get_autoscaley_on(),
                                map_xlim=list(ax.get_xlim()), map_ylim=list(ax.get_ylim()))
            self._auto_color()
        finally:
            self.updating = False
        self._sync_fields()
        self.page.save_state()
        self.page.canvas.draw_idle()

    def _image_changed(self, mesh):
        if self.updating:
            return
        limits = mesh.get_clim()
        if self.last_clim is not None and limits != self.last_clim:
            # The Matplotlib figure editor also allows manual color limits.
            self.options.update(clim_auto=False, clim=list(limits))
            self.last_clim = limits
            self._sync_fields()
            self.page.save_state()

    def apply(self, options):
        proposed = {**self.options, **options}
        for mode, key in (('map_x_auto', 'map_xlim'), ('map_y_auto', 'map_ylim'), ('clim_auto', 'clim')):
            if not proposed[mode]:
                limits = proposed.get(key, [])
                if len(limits) != 2 or not all(math.isfinite(float(v)) for v in limits) or limits[0] == limits[1]:
                    label = {'map_xlim': 'X range', 'map_ylim': 'Y range', 'clim': 'Color range'}[key]
                    raise ValueError(f'{label}: enter two different finite values')
                if key == 'clim' and limits[0] > limits[1]:
                    raise ValueError('Color minimum must be less than maximum')
        if self.page._displayed_primary is None or self.page._colorbar is None:
            return
        self.options = proposed
        self.updating = True
        try:
            ax = self.page.map_ax
            for axis, mode, key in (('x', 'map_x_auto', 'map_xlim'), ('y', 'map_y_auto', 'map_ylim')):
                if proposed[mode]:
                    ax.autoscale(enable=True, axis=axis, tight=True)
                    getattr(ax, f'get_{axis}lim')()  # resolve deferred Matplotlib autoscaling
                else:
                    getattr(ax, f'set_{axis}lim')(proposed[key])
            if proposed['clim_auto']:
                self._auto_color()
            else:
                self._set_clim(proposed['clim'])
            self.options.update(map_xlim=list(ax.get_xlim()), map_ylim=list(ax.get_ylim()))
        finally:
            self.updating = False
        self._sync_fields()
        self.page.save_state()
        self.page.canvas.draw_idle()

    def set_visible(self, visible):
        self._ranges_visible = visible
        for row in self.rows.values():
            row.setVisible(visible)
        self._sync_errors()

    def _sync_errors(self):
        self.error_label.setText('\n'.join(self._errors.values()))
        self.error_label.setVisible(self._ranges_visible and bool(self._errors))

    def _sync_fields(self):
        primary = self.page._displayed_primary
        descriptions = ({'map_xlim': (primary.x_name, primary.x_unit),
                         'map_ylim': (primary.y_name, primary.y_unit),
                         'clim': (primary.signal, primary.signal_unit)} if primary is not None else
                        {'map_xlim': ('X', ''), 'map_ylim': ('Y', ''), 'clim': ('Signal', '')})
        for key, auto in self.modes.items():
            mode, edits = self.mode_controls[key], self.bound_edits[key]
            name, unit = descriptions[key]
            label = f'{name} ({unit})' if unit else name
            mode.setAccessibleName(label + ' range mode')
            self.unit_labels[key].setText(unit)
            if key not in self._drafts:
                with QtCore.QSignalBlocker(mode):
                    mode.setCurrentIndex(0 if self.options[auto] else 1)
                for edit, value in zip(edits, self.options.get(key, [])):
                    edit.setText(f'{value:.6g}')
                    edit.setCursorPosition(0)
            for index, edit in enumerate(edits):
                edit.setAccessibleName(label + (' minimum' if index == 0 else ' maximum'))
                edit.setReadOnly(mode.currentIndex() == 0)
                values = self.options.get(key, [])
                bound = f'Applied value: {values[index]:.17g} {unit}\n' if len(values) == 2 else ''
                edit.setToolTip(bound + 'Auto shows the measured range. Select Fixed to edit; press Enter or leave the field to apply. Scientific notation is accepted.')

    def _mode_changed(self, key):
        if self.updating:
            return
        self._drafts.add(key)
        for edit in self.bound_edits[key]:
            edit.setReadOnly(self.mode_controls[key].currentIndex() == 0)
        self._commit_range(key, force=True)

    def _commit_range(self, key, force=False):
        if self.updating or self.page._displayed_primary is None:
            return
        edits = self.bound_edits[key]
        current = self.options.get(key, [])
        if not force and key not in self._drafts and [edit.text() for edit in edits] == [f'{value:.6g}' for value in current]:
            return
        options = {self.modes[key]: self.mode_controls[key].currentIndex() == 0}
        try:
            if not options[self.modes[key]]:
                try:
                    # Preserve full precision when fixing the displayed auto
                    # range; the compact text is only its formatted preview.
                    options[key] = [value if edit.text() == f'{value:.6g}' else float(edit.text().strip().replace('−', '-'))
                                    for edit, value in zip(edits, current)]
                except ValueError:
                    raise ValueError('Enter numeric bounds, for example -1.5 or 3e-12') from None
            self.apply(options)
        except ValueError as exc:
            self._drafts.add(key)
            self._errors[key] = str(exc)
            self._sync_errors()
            return
        self._drafts.discard(key)
        self._errors.pop(key, None)
        self._sync_errors()
        self._sync_fields()
