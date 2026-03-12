# transport_UI.py
from __future__ import annotations
import sys, os, time, math, json, datetime, re, csv
from dataclasses import dataclass
from typing import Optional, List, Tuple

from PyQt6 import QtCore, QtWidgets
from PyQt6.QtCore import Qt, pyqtSignal, QObject

# Matplotlib canvas
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

# CSVs are written with Python's csv module; no dataProcessing dependency.

from instruments import DaqCard, Keithley2400VoltMode, SP2300


# Constants
V_LIMIT = 20.0

@dataclass
class Connections:
    gate1: str = "GPIB1::23::INSTR"
    gate2: str = "GPIB1::03::INSTR"
    gate3: str = "GPIB1::07::INSTR"
    daq_dev: str = "Dev1"
    mono: str = "ASRL13::INSTR"

@dataclass
class SaveRoot:
    user: str = "User"
    sample: str = "YZ315"
    base: str = r"D:\\photocurrent\\data"
    def path(self) -> str:
        p = os.path.join(self.base, self.user, self.sample); os.makedirs(p, exist_ok=True); return p

# Helpers
def _sanitize_base(s: str) -> str:
    return re.sub(r'[^-_.A-Za-z0-9]+', '_', s).strip('_')

def clamp(v: float, lo: float, hi: float) -> float: return max(lo, min(hi, v))

def _safe(obj, method, *args, **kw):
    try: getattr(obj, method)(*args, **kw)
    except Exception: pass

def _frange_inc(start: float, stop: float, step: float) -> List[float]:
    if step == 0: return [start]
    n = int(math.floor((stop - start) / step + 0.5))
    if n < 0: n = 0
    vals = []
    cur = start
    for _ in range(n+1):
        vals.append(round(cur, 12)); cur += step
    # ensure inclusive bound
    if (step > 0 and vals[-1] < stop) or (step < 0 and vals[-1] > stop):
        vals.append(stop)
    return vals

class PlotWidget(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.fig = Figure(figsize=(5,3))
        self.canvas = FigureCanvas(self.fig)
        self.ax = self.fig.add_subplot(111)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0,0,0,0); lay.addWidget(self.canvas)
        self.clear()
    def clear(self):
        self.ax.clear(); self.ax.grid(True); self.canvas.draw_idle()

# Worker base
class RunWorker(QObject):
    point = pyqtSignal(float, float)
    status = pyqtSignal(str)
    log = pyqtSignal(str)
    progress = pyqtSignal(float)
    finished = pyqtSignal(str)
    error = pyqtSignal(str)
    clear_plot = pyqtSignal()
    def __init__(self):
        super().__init__()
        self._stop = False
        self._pause = False
    def request_stop(self): self._stop = True
    def request_pause(self, p: bool): self._pause = p
    def check_abort_pause(self):
        if self._stop: raise RuntimeError("Stopped")
        while self._pause: time.sleep(0.05)

# ----------------- Dual-gate Vds Sweep -----------------
class DualGateParams(QtCore.QObject):
    def __init__(self):
        super().__init__()
        self.base_name = "dual_gate"
        self.vds_source = "Keithley 2400" # or NI DAQ aoX
        self.vds_start = 0.0; self.vds_stop = 0.5; self.vds_step = 0.01; self.vds_ramp = 0.05
        self.vtg_set=0.0; self.vbg_set=0.0
        self.vg_ramp=0.2; self.delay=0.5; self.n_sample=3
        self.plot_choice="Ids_DC"
        self.ao_channel = 0

class DualGateWorker(RunWorker):
    def __init__(self, params: DualGateParams, save: SaveRoot, conns: Connections, **kw):
        super().__init__(); self.p=params; self.save=save; self.conns=conns
        self.g1=kw.get('g1'); self.g2=kw.get('g2'); self.g3=kw.get('g3'); self.daq=kw.get('daq')
        self.plot_choice=kw.get('plot_choice'); self.amp_rate=kw.get('amp_rate',1e7); self.lkn_rate=kw.get('lkn_rate',100.0)

    @QtCore.pyqtSlot()
    def run(self):
        try:
            # --- MODIFIED: Relaxed checks ---
            # We strictly need DAQ to read data.
            if self.daq is None:
                self.error.emit("Required session missing: DAQ"); return
            # We allow running without G1 or G2.
            # --------------------------------

            # clamp
            for f in ("vds_start","vds_stop","vtg_set","vbg_set"):
                setattr(self.p, f, clamp(getattr(self.p,f), -V_LIMIT, V_LIMIT))

            # Build filename with active gates info
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            tag_src = "Vdsketh" if self.p.vds_source=="Keithley 2400" else f"Vdsdaq_ao{self.p.ao_channel}"
            
            g1_tag = "Tg" if self.g1 else "NoTg"
            g2_tag = "Bg" if self.g2 else "NoBg"
            
            stem = f"{_sanitize_base(self.p.base_name)}_{g1_tag}_{g2_tag}_{tag_src}_Vtg{self.p.vtg_set:+.3f}V_Vbg{self.p.vbg_set:+.3f}V_{ts}"
            out_dir = self.save.path()
            csv_path = os.path.join(out_dir, stem + ".csv")
            self.log.emit(f"Save -> {csv_path}")

            # CSV
            with open(csv_path, 'a', newline='', buffering=1, encoding='utf-8') as f:
                w = csv.writer(f)
                w.writerow(['Vg1','Vg2','Vds','raw_X','raw_Y','raw_DC','Ids_X','Ids_Y','Ids_DC'])
                w.writerow(['V','V','V','A','A','A','A','A','A'])

                # set fixed gates (if connected)
                _safe(self.g1, 'ramp_voltage', self.p.vtg_set, self.p.vg_ramp)
                _safe(self.g2, 'ramp_voltage', self.p.vbg_set, self.p.vg_ramp)

                # sweep vds
                step = self.p.vds_step if self.p.vds_stop >= self.p.vds_start else -abs(self.p.vds_step)
                vseq = _frange_inc(self.p.vds_start, self.p.vds_stop, step)
                total = max(1, len(vseq))
                
                for i, vds in enumerate(vseq, start=1):
                    self.check_abort_pause()
                    if self.p.vds_source == "Keithley 2400":
                        if self.g3 is None: self.error.emit("Gate3 (Keithley Vds) not connected"); return
                        _safe(self.g3, 'ramp_voltage', vds, self.p.vds_ramp)
                    else:
                        _safe(self.daq, 'ramp_voltage', self.p.ao_channel, vds, self.p.vds_ramp)
                    
                    time.sleep(max(0.0, self.p.delay))

                    raw_X=raw_Y=raw_DC=0.0
                    for _ in range(int(self.p.n_sample)):
                        self.check_abort_pause()
                        self.daq.acquire()
                        raw_X += self.daq.get_ai_value(0)
                        raw_Y += self.daq.get_ai_value(1)
                        raw_DC += self.daq.get_ai_value(2)
                    raw_X/=self.p.n_sample; raw_Y/=self.p.n_sample; raw_DC/=self.p.n_sample

                    ids_X = raw_X/(self.amp_rate*self.lkn_rate)
                    ids_Y = raw_Y/(self.amp_rate*self.lkn_rate)
                    ids_DC= raw_DC/self.amp_rate

                    w.writerow([self.p.vtg_set, self.p.vbg_set, vds, raw_X, raw_Y, raw_DC, ids_X, ids_Y, ids_DC])
                    try: f.flush(); os.fsync(f.fileno())
                    except Exception: pass

                    y = ids_DC if self.plot_choice=='Ids_DC' else (ids_X if self.plot_choice=='Ids_X' else ids_Y)
                    self.point.emit(vds, y)
                    self.progress.emit(i/total)

            self.finished.emit(csv_path)
        except Exception as ex:
            self.error.emit(str(ex))
        finally:
            try:
                _safe(self.g1, 'ramp_voltage', 0.0, self.p.vg_ramp)
                _safe(self.g2, 'ramp_voltage', 0.0, self.p.vg_ramp)
                if self.p.vds_source == "Keithley 2400" and self.g3 is not None:
                    _safe(self.g3, 'ramp_voltage', 0.0, self.p.vds_ramp)
                else:
                    _safe(self.daq, 'ramp_voltage', self.p.ao_channel, 0.0, self.p.vds_ramp)
            except Exception: pass
            self.log.emit("Outputs returned to 0 V; sessions kept open.")

class DualGateTab(QtWidgets.QWidget):
    def __init__(self, save: SaveRoot, conns: Connections, get_global_rates_callable=None):
        super().__init__()
        self.save = save; self.conns = conns
        self.get_global_rates = get_global_rates_callable or (lambda: (1e7,100.0))
        self.p = DualGateParams()
        self.s_g1=self.s_g2=self.s_g3=self.s_daq=None
        self.worker_thread=None; self.worker=None
        self._build_ui(); self._wire()

    # Exposed to others for AO items
    def get_ao_items_if_available(self) -> List[str]:
        items = []
        try:
            from nidaqmx.system import System
            sys = System.local()
            dev = next((d for d in sys.devices if d.name == self.conns.daq_dev), None)
            if dev:
                items = [ch.name.split('/')[-1] for ch in dev.ao_physical_chans]
        except Exception:
            pass
        if not items: items = ["ao0","ao1"]
        return items

    def _build_ui(self):
        # --- Main Layout ---
        main_layout = QtWidgets.QHBoxLayout(self)
        
        # --- Left Column: Controls ---
        scroll = QtWidgets.QScrollArea(); scroll.setWidgetResizable(True)
        ctl_widget = QtWidgets.QWidget(); scroll.setWidget(ctl_widget)
        ctl_layout = QtWidgets.QVBoxLayout(ctl_widget)
        
        # 1. File & Source
        grp_file = QtWidgets.QGroupBox("Configuration")
        form_file = QtWidgets.QFormLayout()
        self.ed_base = QtWidgets.QLineEdit(self.p.base_name)
        self.cbo_source = QtWidgets.QComboBox(); self.cbo_source.addItems(["Keithley 2400"])
        self.cbo_y = QtWidgets.QComboBox(); self.cbo_y.addItems(["Ids_DC","Ids_X","Ids_Y"])
        form_file.addRow("Filename:", self.ed_base)
        form_file.addRow("Vds Source:", self.cbo_source)
        form_file.addRow("Plot Axis:", self.cbo_y)
        grp_file.setLayout(form_file)
        ctl_layout.addWidget(grp_file)

        # 2. Vds Settings
        grp_vds = QtWidgets.QGroupBox("Vds Sweep")
        form_vds = QtWidgets.QFormLayout()
        self.sp_vds_start = QtWidgets.QDoubleSpinBox(); self._volt(self.sp_vds_start, 0.0)
        self.sp_vds_stop  = QtWidgets.QDoubleSpinBox(); self._volt(self.sp_vds_stop, 0.5)
        self.sp_vds_step  = QtWidgets.QDoubleSpinBox(); self.sp_vds_step.setDecimals(3); self.sp_vds_step.setRange(1e-3, 5.0); self.sp_vds_step.setValue(0.01)
        self.sp_vds_ramp  = QtWidgets.QDoubleSpinBox(); self.sp_vds_ramp.setDecimals(3); self.sp_vds_ramp.setRange(1e-3, 5.0); self.sp_vds_ramp.setValue(0.05)
        form_vds.addRow("Start (V):", self.sp_vds_start)
        form_vds.addRow("Stop (V):", self.sp_vds_stop)
        form_vds.addRow("Step (V):", self.sp_vds_step)
        form_vds.addRow("Ramp (V):", self.sp_vds_ramp)
        grp_vds.setLayout(form_vds)
        ctl_layout.addWidget(grp_vds)

        # 3. Gate Settings (With Manual Buttons)
        grp_gate = QtWidgets.QGroupBox("Gate Static Settings")
        lay_gate = QtWidgets.QVBoxLayout()
        
        # Helper for row with button
        def add_manual_row(label, spinbox, slot):
            h = QtWidgets.QHBoxLayout()
            h.addWidget(QtWidgets.QLabel(label))
            h.addWidget(spinbox)
            btn = QtWidgets.QPushButton("Set")
            btn.setFixedWidth(40)
            btn.clicked.connect(slot)
            h.addWidget(btn)
            return h

        self.sp_vtg = QtWidgets.QDoubleSpinBox(); self._volt(self.sp_vtg, 0.0)
        self.sp_vbg = QtWidgets.QDoubleSpinBox(); self._volt(self.sp_vbg, 0.0)
        self.sp_vg_ramp = QtWidgets.QDoubleSpinBox(); self.sp_vg_ramp.setDecimals(3); self.sp_vg_ramp.setRange(1e-3,5.0); self.sp_vg_ramp.setValue(0.2)

        lay_gate.addLayout(add_manual_row("Vtg (V):", self.sp_vtg, self.on_set_vtg))
        lay_gate.addLayout(add_manual_row("Vbg (V):", self.sp_vbg, self.on_set_vbg))
        
        # Add Vg Ramp rate in a simple row
        h_ramp = QtWidgets.QHBoxLayout(); h_ramp.addWidget(QtWidgets.QLabel("Gate Ramp:"))
        h_ramp.addWidget(self.sp_vg_ramp)
        lay_gate.addLayout(h_ramp)
        grp_gate.setLayout(lay_gate)
        ctl_layout.addWidget(grp_gate)

        # 4. Timing
        grp_time = QtWidgets.QGroupBox("Timing")
        form_time = QtWidgets.QFormLayout()
        self.sp_delay = QtWidgets.QDoubleSpinBox(); self.sp_delay.setDecimals(3); self.sp_delay.setRange(0.0,10.0); self.sp_delay.setValue(0.5)
        self.sp_nsamp = QtWidgets.QSpinBox(); self.sp_nsamp.setRange(1,1000); self.sp_nsamp.setValue(3)
        form_time.addRow("Delay (s):", self.sp_delay)
        form_time.addRow("Samples:", self.sp_nsamp)
        grp_time.setLayout(form_time)
        ctl_layout.addWidget(grp_time)

        # 5. Connection Status
        grp_stat = QtWidgets.QGroupBox("Status")
        form_stat = QtWidgets.QFormLayout()
        self.lbl_g1 = QtWidgets.QLabel("—"); self.lbl_g2=QtWidgets.QLabel("—"); self.lbl_g3=QtWidgets.QLabel("—"); self.lbl_daq=QtWidgets.QLabel("—")
        form_stat.addRow("Gate1:", self.lbl_g1)
        form_stat.addRow("Gate2:", self.lbl_g2)
        form_stat.addRow("G3/Vds:", self.lbl_g3)
        form_stat.addRow("DAQ:", self.lbl_daq)
        grp_stat.setLayout(form_stat)
        ctl_layout.addWidget(grp_stat)

        # Buttons
        self.btn_connect = QtWidgets.QPushButton("Connect")
        self.btn_disconnect = QtWidgets.QPushButton("Disconnect All")
        self.btn_ao_test = QtWidgets.QPushButton("AO Test (+0.1V)")
        ctl_layout.addWidget(self.btn_connect)
        ctl_layout.addWidget(self.btn_disconnect)
        ctl_layout.addWidget(self.btn_ao_test)
        
        ctl_layout.addStretch()
        
        # Set column width policy
        ctl_widget.setMinimumWidth(300)
        ctl_widget.setMaximumWidth(350)

        # --- Right Column: Plot & Log ---
        right_widget = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right_widget)
        self.plot = PlotWidget(); self.plot.ax.set_xlabel("Vds (V)"); self.plot.ax.set_ylabel("Ids (A)")
        
        self.btn_start = QtWidgets.QPushButton("START RUN"); self.btn_start.setMinimumHeight(40); self.btn_start.setStyleSheet("font-weight: bold; background-color: #e0f7fa;")
        self.btn_stop = QtWidgets.QPushButton("STOP"); self.btn_stop.setMinimumHeight(40)
        self.progress = QtWidgets.QProgressBar()
        self.lbl_status = QtWidgets.QLabel("Idle")
        self.lbl_status.setStyleSheet("font-size: 14px; font-weight: bold; color: gray; border: 1px solid gray; padding: 4px;")

        run_layout = QtWidgets.QHBoxLayout()
        run_layout.addWidget(self.btn_start); run_layout.addWidget(self.btn_stop); run_layout.addWidget(self.progress)

        self.log = QtWidgets.QPlainTextEdit(); self.log.setReadOnly(True)
        
        right_layout.addWidget(self.plot, 2)
        right_layout.addLayout(run_layout)
        right_layout.addWidget(self.lbl_status)
        right_layout.addWidget(self.log, 1)

        main_layout.addWidget(scroll)
        main_layout.addWidget(right_widget)

    def _volt(self, sp, val): sp.setDecimals(3); sp.setRange(-V_LIMIT, V_LIMIT); sp.setSingleStep(0.1); sp.setValue(val)

    def _wire(self):
        self.btn_connect.clicked.connect(self.on_connect)
        self.btn_disconnect.clicked.connect(self.on_disconnect_all)
        self.btn_start.clicked.connect(self.start_run)
        self.btn_stop.clicked.connect(self.stop_run)
        self.btn_ao_test.clicked.connect(self.on_ao_test)

    # --- Manual Set Slots ---
    def on_set_vtg(self):
        if self.s_g1:
            try:
                val = self.sp_vtg.value()
                self.s_g1.ramp_voltage(val, 0.5) # Fast-ish ramp for manual set
                self.log.appendPlainText(f"[Manual] Gate1 set to {val} V")
            except Exception as e: self.log.appendPlainText(f"Error setting G1: {e}")
        else: self.log.appendPlainText("Gate1 not connected.")

    def on_set_vbg(self):
        if self.s_g2:
            try:
                val = self.sp_vbg.value()
                self.s_g2.ramp_voltage(val, 0.5)
                self.log.appendPlainText(f"[Manual] Gate2 set to {val} V")
            except Exception as e: self.log.appendPlainText(f"Error setting G2: {e}")
        else: self.log.appendPlainText("Gate2 not connected.")

    def _ensure_sessions(self) -> bool:
        mw = self.window()
        if hasattr(mw, 'refresh_models_from_ui'): mw.refresh_models_from_ui()
        self.log.appendPlainText(f"Using addresses -> G1:{self.conns.gate1}  G2:{self.conns.gate2}  G3:{self.conns.gate3}  DAQ:{self.conns.daq_dev}")
        
        # Connect G1
        if self.s_g1 is None:
            try:
                if not self.conns.gate1: raise ValueError("Address empty")
                self.s_g1 = Keithley2400VoltMode('g1', self.conns.gate1, curr_comp=1e-7, volt_comp=40)
                self.s_g1.connect()
                self.lbl_g1.setText("OK"); self.lbl_g1.setStyleSheet("color: green; font-weight: bold")
            except Exception as ex:
                self.lbl_g1.setText(f"Err"); self.lbl_g1.setStyleSheet("color: red")
                self.log.appendPlainText(f"Gate 1 failed: {ex}"); self.s_g1 = None

        # Connect G2
        if self.s_g2 is None:
            try:
                if not self.conns.gate2: raise ValueError("Address empty")
                self.s_g2 = Keithley2400VoltMode('g2', self.conns.gate2, curr_comp=1e-7, volt_comp=40)
                self.s_g2.connect()
                self.lbl_g2.setText("OK"); self.lbl_g2.setStyleSheet("color: green; font-weight: bold")
            except Exception as ex:
                self.lbl_g2.setText(f"Err"); self.lbl_g2.setStyleSheet("color: red")
                self.log.appendPlainText(f"Gate 2 failed: {ex}"); self.s_g2 = None

        # Connect G3 (Vds)
        if self.s_g3 is None:
            try:
                if not self.conns.gate3: raise ValueError("Address empty")
                self.s_g3 = Keithley2400VoltMode('g3', self.conns.gate3, curr_comp=1e-6, volt_comp=20)
                self.s_g3.connect()
                self.lbl_g3.setText("OK"); self.lbl_g3.setStyleSheet("color: green; font-weight: bold")
            except Exception as ex:
                self.lbl_g3.setText(f"Err"); self.lbl_g3.setStyleSheet("color: red")

        # Connect DAQ (Required)
        try:
            if self.s_daq is None:
                ao_items = self.get_ao_items_if_available()
                ao_indexes=[int(i[2:]) for i in ao_items if i.startswith("ao")]
                self.s_daq = DaqCard(address=self.conns.daq_dev, ao_channel_indexes=ao_indexes, ai_channel_indexes=[0,1,2,3], read_delay=0.5); self.s_daq.connect(); 
                self.lbl_daq.setText(f"OK"); self.lbl_daq.setStyleSheet("color: green; font-weight: bold")
                # populate source
                items = ["Keithley 2400"] + [f"NI DAQ {a}" for a in ao_items]
                self.cbo_source.blockSignals(True); cur=self.cbo_source.currentText(); self.cbo_source.clear(); self.cbo_source.addItems(items);
                if cur in items: self.cbo_source.setCurrentText(cur)
                self.cbo_source.blockSignals(False)
        except Exception as ex:
            QtWidgets.QMessageBox.critical(self, "Connect error", str(ex)); return False
            
        return True

    def on_connect(self):
        if self._ensure_sessions(): QtWidgets.QMessageBox.information(self, "Connect", "Connected.")

    def on_disconnect_all(self):
        for obj, lab in ((self.s_g1,self.lbl_g1),(self.s_g2,self.lbl_g2),(self.s_g3,self.lbl_g3),(self.s_daq,self.lbl_daq)):
            _safe(obj,'disconnect'); _safe(obj,'close'); lab.setText("—"); lab.setStyleSheet("")
        self.s_g1=self.s_g2=self.s_g3=self.s_daq=None
        self.log.appendPlainText("All sessions disconnected by user.")

    def collect_params(self):
        self.p.base_name = self.ed_base.text()
        src_text = self.cbo_source.currentText()
        if src_text.startswith("NI DAQ "):
            self.p.vds_source = "NI DAQ AO"
            try: self.p.ao_channel = int(src_text.split()[-1].replace('ao',''))
            except Exception: self.p.ao_channel = 0
        else:
            self.p.vds_source = "Keithley 2400"
        self.p.vds_start = float(self.sp_vds_start.value()); self.p.vds_stop=float(self.sp_vds_stop.value()); self.p.vds_step=float(self.sp_vds_step.value()); self.p.vds_ramp=float(self.sp_vds_ramp.value())
        self.p.vtg_set=float(self.sp_vtg.value()); self.p.vbg_set=float(self.sp_vbg.value()); self.p.vg_ramp=float(self.sp_vg_ramp.value())
        self.p.delay=float(self.sp_delay.value()); self.p.n_sample=int(self.sp_nsamp.value()); self.p.plot_choice=self.cbo_y.currentText()

    def start_run(self):
        if self.worker_thread: QtWidgets.QMessageBox.warning(self, "Busy", "Run already in progress"); return
        if not self._ensure_sessions(): return
        self.collect_params(); self.plot.clear()
        self.plot.ax.set_xlabel("Vds (V)"); self.plot.ax.set_ylabel(self.p.plot_choice + " (A)"); self.plot.canvas.draw_idle()
        amp_rate, lkn_rate = (1e7, 100.0)
        try:
            mw = self.window()
            if hasattr(mw, 'conn_dock') and hasattr(mw.conn_dock, 'get_rates'): amp_rate, lkn_rate = mw.conn_dock.get_rates()
        except Exception: pass
        try:
            self.worker = DualGateWorker(self.p, self.save, self.conns, g1=self.s_g1, g2=self.s_g2, g3=self.s_g3, daq=self.s_daq, plot_choice=self.p.plot_choice, amp_rate=amp_rate, lkn_rate=lkn_rate)
            self.worker_thread = QtCore.QThread(); self.worker.moveToThread(self.worker_thread)
            self.worker_thread.started.connect(self.worker.run)
            self.worker.point.connect(self.on_point); self.worker.progress.connect(lambda p: self.progress.setValue(int(p*100)))
            self.worker.status.connect(self._update_status); self.worker.log.connect(self.log.appendPlainText)
            self.worker.finished.connect(self.on_finished); self.worker.error.connect(self.on_error)
            self.worker.finished.connect(self.worker_thread.quit); self.worker.error.connect(self.worker_thread.quit)
            self.worker_thread.finished.connect(self._cleanup_thread)
            self._update_status("Running..."); self.progress.setValue(0); self.worker_thread.start(); self.log.appendPlainText("[start] Worker thread started.")
        except Exception as ex:
            self.log.appendPlainText(f"[start] ERROR while creating/starting worker: {ex}"); return

    def _update_status(self, msg):
        self.lbl_status.setText(msg)
        if "error" in msg.lower(): self.lbl_status.setStyleSheet("color: white; background-color: red; padding: 4px;")
        elif "running" in msg.lower(): self.lbl_status.setStyleSheet("color: white; background-color: green; padding: 4px;")
        else: self.lbl_status.setStyleSheet("font-size: 14px; font-weight: bold; color: gray; border: 1px solid gray; padding: 4px;")

    def stop_run(self):
        if self.worker: self.log.appendPlainText("Stop requested by user."); self.worker.request_stop()

    def _cleanup_thread(self):
        if self.worker: self.worker.deleteLater(); self.worker=None
        self.worker_thread=None

    def on_point(self, x, y):
        ax = self.plot.ax
        if ax.lines:
            ln=ax.lines[0]; xs=list(ln.get_xdata()); ys=list(ln.get_ydata()); xs.append(x); ys.append(y); ln.set_data(xs, ys)
        else:
            ax.plot([x],[y], marker='o')
        ax.relim(); ax.autoscale_view(); self.plot.canvas.draw_idle()

    def on_error(self, msg): self._update_status(f"Error: {msg}"); self.log.appendPlainText("ERROR: "+msg)
    def on_finished(self, csv_path: str):
        self._update_status(f"Finished"); self.log.appendPlainText(f"Saved: {csv_path}")

    def on_ao_test(self):
        if not self._ensure_sessions(): return
        try:
            items = self.get_ao_items_if_available()
            idx = int(items[0][2:]) if items else 0
            _safe(self.s_daq, 'ramp_voltage', idx, 0.1, 0.01); time.sleep(0.1); _safe(self.s_daq, 'ramp_voltage', idx, 0.0, 0.01)
            QtWidgets.QMessageBox.information(self, "AO Test", f"Pulsed +0.1 V on {items[0] if items else 'ao0'}")
        except Exception as ex:
            QtWidgets.QMessageBox.critical(self, "AO Test", str(ex))

# ----------------- Co-sweep -----------------
class CoParams(QtCore.QObject):
    def __init__(self):
        super().__init__()
        self.base_name="dual_gate_cosweep"
        self.vds_source="Keithley 2400"; self.ao_channel=0; self.vds_set=0.0; self.vds_ramp=0.05
        self.vtg_start=0.0; self.vtg_stop=1.0; self.vtg_step=0.1
        self.vbg_start=0.0; self.vbg_stop=1.0; self.ratio=1.0
        self.vg_ramp=0.2; self.delay=0.5; self.n_sample=3
        self.inner="Vbg"  # Vbg or Vtg
        self.plot_choice="Ids_DC"

class CoWorker(RunWorker):
    def __init__(self, params: CoParams, save: SaveRoot, conns: Connections, **kw):
        super().__init__(); self.p=params; self.save=save; self.conns=conns
        self.g1=kw.get('g1'); self.g2=kw.get('g2'); self.g3=kw.get('g3'); self.daq=kw.get('daq')
        self.plot_choice=kw.get('plot_choice'); self.amp_rate=kw.get('amp_rate',1e7); self.lkn_rate=kw.get('lkn_rate',100.0)

    @QtCore.pyqtSlot()
    
    @QtCore.pyqtSlot()
    def run(self):
        try:
            if self.daq is None: self.error.emit("DAQ missing."); return
            vds_obj = self.daq if self.p.vds_source.startswith("NI DAQ") else self.g3
            
            # --- 1. Define Axes (Fast vs Slow) ---
            # We enforce a Grid logic: Fast Loop inside Slow Loop
            fast_axis = self.p.axis_fast
            slow_axis = self.p.axis_slow

            # Helper to get sequence
            def get_seq(name):
                if name == "Vtg": start, stop, step = self.p.vtg_start, self.p.vtg_stop, self.p.vtg_step
                elif name == "Vbg": start, stop, step = self.p.vbg_start, self.p.vbg_stop, self.p.vbg_step
                elif name == "Vds": start, stop, step = self.p.vds_start, self.p.vds_stop, self.p.vds_step
                else: return [0.0] # None case

                s = step if stop >= start else -abs(step)
                if abs(s) < 1e-9: return [start]
                return _frange_inc(start, stop, s)

            fast_seq = get_seq(fast_axis)
            slow_seq = get_seq(slow_axis) if slow_axis != "None" else [0.0]

            # --- 2. File Setup ---
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            # Naming: "Megasweep" to distinguish from the old linked line
            stem = f"{_sanitize_base(self.p.base_name)}_Megasweep_{fast_axis}_{slow_axis}_{ts}"
            out_dir = self.save.path(); csv_path = os.path.join(out_dir, stem + ".csv")
            self.log.emit(f"Save -> {csv_path}")

            with open(csv_path, 'a', newline='', buffering=1, encoding='utf-8') as f:
                w = csv.writer(f)
                w.writerow(['Vg1','Vg2','Vds','raw_X','raw_Y','raw_DC','Ids_X','Ids_Y','Ids_DC','Doping','Efield'])
                w.writerow(['V','V','V','A','A','A','A','A','A','V','V'])

                # Initialize Fixed Voltages (if not swept)
                active_axes = [fast_axis, slow_axis]
                if "Vtg" not in active_axes: _safe(self.g1, 'ramp_voltage', self.p.vtg_start, self.p.vg_ramp)
                if "Vbg" not in active_axes: _safe(self.g2, 'ramp_voltage', self.p.vbg_start, self.p.vg_ramp)
                if "Vds" not in active_axes: 
                     # Set Vds start
                     if self.p.vds_source.startswith("NI DAQ"): _safe(self.daq, 'ramp_voltage', self.p.ao_channel, self.p.vds_start, self.p.vds_ramp)
                     else: _safe(self.g3, 'ramp_voltage', self.p.vds_start, self.p.vds_ramp)

                cnt = 0; total = len(fast_seq) * len(slow_seq)
                self.clear_plot.emit()

                # --- 3. The Megasweep Nested Loop ---
                for s_val in slow_seq:
                    # Move Slow Axis
                    self.set_volt(slow_axis, s_val)
                    
                    for f_val in fast_seq:
                        self.check_abort_pause()
                        
                        # Move Fast Axis
                        self.set_volt(fast_axis, f_val)
                        
                        # Wait
                        time.sleep(self.p.delay)

                        # Measure
                        raw_X=0; raw_Y=0; raw_DC=0
                        for _ in range(self.p.n_sample):
                            self.daq.acquire()
                            raw_X+=self.daq.get_ai_value(0); raw_Y+=self.daq.get_ai_value(1); raw_DC+=self.daq.get_ai_value(2)
                        raw_X/=self.p.n_sample; raw_Y/=self.p.n_sample; raw_DC/=self.p.n_sample
                        
                        ids_X=raw_X/(self.amp_rate*self.lkn_rate); ids_Y=raw_Y/(self.amp_rate*self.lkn_rate); ids_DC=raw_DC/self.amp_rate

                        # --- 4. The Ratio Calculation (For Data Saving) ---
                        # Determine current Vtg/Vbg/Vds values
                        curr_vtg = f_val if fast_axis=="Vtg" else (s_val if slow_axis=="Vtg" else self.p.vtg_start)
                        curr_vbg = f_val if fast_axis=="Vbg" else (s_val if slow_axis=="Vbg" else self.p.vbg_start)
                        curr_vds = f_val if fast_axis=="Vds" else (s_val if slow_axis=="Vds" else self.p.vds_start)
                        
                        # Use the user's Ratio for Doping/Field
                        # Doping = Ratio*Vtg + Vbg
                        # Efield = Ratio*Vtg - Vbg
                        doping = (self.p.ratio * curr_vtg) + curr_vbg
                        efield = (self.p.ratio * curr_vtg) - curr_vbg

                        w.writerow([curr_vtg, curr_vbg, curr_vds, raw_X, raw_Y, raw_DC, ids_X, ids_Y, ids_DC, doping, efield])
                        
                        # Plot
                        y_val = ids_DC if self.plot_choice=='Ids_DC' else (ids_X if self.plot_choice=='Ids_X' else ids_Y)
                        
                        # If "Linked" check is ON, we plot against Doping. If OFF, we plot against Fast Axis.
                        x_plot = doping if self.p.mode == "Linked" else f_val
                        
                        self.point.emit(x_plot, y_val)
                        cnt+=1; self.progress.emit(cnt/total)

            self.finished.emit(csv_path)
        except Exception as e: self.error.emit(str(e))

    def set_volt(self, name, val):
        if name == "Vtg": _safe(self.g1, 'ramp_voltage', val, self.p.vg_ramp)
        elif name == "Vbg": _safe(self.g2, 'ramp_voltage', val, self.p.vg_ramp)
        elif name == "Vds":
            if self.p.vds_source.startswith("NI DAQ"): _safe(self.daq, 'ramp_voltage', self.p.ao_channel, val, self.p.vds_ramp)
            else: _safe(self.g3, 'ramp_voltage', val, self.p.vds_ramp)

class CoSweepTab(QtWidgets.QWidget):
    def __init__(self, save: SaveRoot, conns: Connections, get_global_rates_callable=None, get_ao_items_callable=None):
        super().__init__()
        self.save=save; self.conns=conns
        self.get_global_rates = get_global_rates_callable or (lambda: (1e7,100.0))
        self.get_ao_items = get_ao_items_callable or (lambda: ["ao0","ao1"])
        self.p = CoParams()
        self.s_g1=self.s_g2=self.s_g3=self.s_daq=None
        self.worker_thread=None; self.worker=None
        self._updating_combos = False
        self._build_ui(); self._wire()
        self.on_fast_combo_changed()

    def _build_ui(self):
        main_layout = QtWidgets.QHBoxLayout(self)

        scroll = QtWidgets.QScrollArea(); scroll.setWidgetResizable(True)
        ctl_widget = QtWidgets.QWidget(); scroll.setWidget(ctl_widget)
        ctl_layout = QtWidgets.QVBoxLayout(ctl_widget)
        ctl_layout.setSpacing(5); ctl_layout.setContentsMargins(5,5,5,5)

        # 1. Config
        grp_conf = QtWidgets.QGroupBox("Configuration")
        form_conf = QtWidgets.QFormLayout()
        form_conf.setContentsMargins(5,5,5,5)
        self.ed_base = QtWidgets.QLineEdit(self.p.base_name)
        self.cbo_source = QtWidgets.QComboBox(); self.cbo_source.addItems(["Keithley 2400"])
        self.cbo_y = QtWidgets.QComboBox(); self.cbo_y.addItems(["Ids_DC","Ids_X","Ids_Y"])
        form_conf.addRow("Filename:", self.ed_base)
        form_conf.addRow("Vds Source:", self.cbo_source)
        form_conf.addRow("Plot Axis:", self.cbo_y)
        grp_conf.setLayout(form_conf); ctl_layout.addWidget(grp_conf)

        # 2. Variables
        grp_vars = QtWidgets.QGroupBox("Sweep Variables")
        lay_vars = QtWidgets.QGridLayout(); lay_vars.setSpacing(5)
        lay_vars.addWidget(QtWidgets.QLabel("Start (Fixed)"),0,1); lay_vars.addWidget(QtWidgets.QLabel("Stop"),0,2)
        lay_vars.addWidget(QtWidgets.QLabel("Step"),0,3); lay_vars.addWidget(QtWidgets.QLabel("Set"),0,4)

        # VTG
        self.sp_vtg_start=QtWidgets.QDoubleSpinBox(); self._volt(self.sp_vtg_start, 0.0)
        self.sp_vtg_stop=QtWidgets.QDoubleSpinBox(); self._volt(self.sp_vtg_stop, 1.0)
        self.sp_vtg_step=QtWidgets.QDoubleSpinBox(); self._volt(self.sp_vtg_step, 0.1)
        btn_vtg = QtWidgets.QPushButton("Go"); btn_vtg.setFixedWidth(35); btn_vtg.clicked.connect(lambda: self.on_set_generic("Vtg"))
        lay_vars.addWidget(QtWidgets.QLabel("Vtg (V):"),1,0); lay_vars.addWidget(self.sp_vtg_start,1,1)
        lay_vars.addWidget(self.sp_vtg_stop,1,2); lay_vars.addWidget(self.sp_vtg_step,1,3); lay_vars.addWidget(btn_vtg,1,4)

        # VBG
        self.sp_vbg_start=QtWidgets.QDoubleSpinBox(); self._volt(self.sp_vbg_start, 0.0)
        self.sp_vbg_stop=QtWidgets.QDoubleSpinBox(); self._volt(self.sp_vbg_stop, 1.0)
        self.sp_vbg_step=QtWidgets.QDoubleSpinBox(); self._volt(self.sp_vbg_step, 0.1)
        btn_vbg = QtWidgets.QPushButton("Go"); btn_vbg.setFixedWidth(35); btn_vbg.clicked.connect(lambda: self.on_set_generic("Vbg"))
        lay_vars.addWidget(QtWidgets.QLabel("Vbg (V):"),2,0); lay_vars.addWidget(self.sp_vbg_start,2,1)
        lay_vars.addWidget(self.sp_vbg_stop,2,2); lay_vars.addWidget(self.sp_vbg_step,2,3); lay_vars.addWidget(btn_vbg,2,4)

        # VDS
        self.sp_vds_start=QtWidgets.QDoubleSpinBox(); self._volt(self.sp_vds_start, 0.0)
        self.sp_vds_stop=QtWidgets.QDoubleSpinBox(); self._volt(self.sp_vds_stop, 0.0)
        self.sp_vds_step=QtWidgets.QDoubleSpinBox(); self._volt(self.sp_vds_step, 0.01)
        btn_vds = QtWidgets.QPushButton("Go"); btn_vds.setFixedWidth(35); btn_vds.clicked.connect(lambda: self.on_set_generic("Vds"))
        lay_vars.addWidget(QtWidgets.QLabel("Vds (V):"),3,0); lay_vars.addWidget(self.sp_vds_start,3,1)
        lay_vars.addWidget(self.sp_vds_stop,3,2); lay_vars.addWidget(self.sp_vds_step,3,3); lay_vars.addWidget(btn_vds,3,4)
        grp_vars.setLayout(lay_vars); ctl_layout.addWidget(grp_vars)

        # 3. Sweep Logic
        grp_logic = QtWidgets.QGroupBox("Sweep Logic")
        form_logic = QtWidgets.QFormLayout()
        
        self.chk_link = QtWidgets.QCheckBox("Link Vtg-Vbg (Diagonal)")
        self.chk_link.setToolTip("Enables Diagonal Sweep. Forces Fast=Vtg OR Vbg. Slow=None (1D).")
        
        self.cbo_fast = QtWidgets.QComboBox(); self.cbo_fast.addItems(["Vtg","Vbg","Vds"])
        self.cbo_slow = QtWidgets.QComboBox(); self.cbo_slow.addItems(["None","Vtg","Vbg","Vds"])
        self.sp_ratio = QtWidgets.QDoubleSpinBox(); self.sp_ratio.setDecimals(4); self.sp_ratio.setRange(-1e4,1e4); self.sp_ratio.setValue(1.0)
        
        form_logic.addRow(self.chk_link)
        form_logic.addRow("Fast Axis (Master):", self.cbo_fast)
        form_logic.addRow("Slow Axis (Outer):", self.cbo_slow)
        form_logic.addRow("Ratio:", self.sp_ratio)
        form_logic.addRow(QtWidgets.QLabel("(Linked: Slave = Ratio * Master)"))
        grp_logic.setLayout(form_logic); ctl_layout.addWidget(grp_logic)

        # 4. Timing
        grp_time = QtWidgets.QGroupBox("Timing")
        lay_time = QtWidgets.QHBoxLayout()
        self.sp_delay = QtWidgets.QDoubleSpinBox(); self.sp_delay.setValue(0.5)
        self.sp_nsamp = QtWidgets.QSpinBox(); self.sp_nsamp.setValue(3)
        self.sp_vg_ramp = QtWidgets.QDoubleSpinBox(); self.sp_vg_ramp.setValue(0.2)
        lay_time.addWidget(QtWidgets.QLabel("Delay:")); lay_time.addWidget(self.sp_delay)
        lay_time.addWidget(QtWidgets.QLabel("Avg:")); lay_time.addWidget(self.sp_nsamp)
        lay_time.addWidget(QtWidgets.QLabel("Ramp:")); lay_time.addWidget(self.sp_vg_ramp)
        grp_time.setLayout(lay_time); ctl_layout.addWidget(grp_time)

        # Connect/Status
        lay_btns = QtWidgets.QHBoxLayout()
        self.btn_connect = QtWidgets.QPushButton("Connect"); self.btn_disconnect = QtWidgets.QPushButton("Disconnect"); self.btn_preview = QtWidgets.QPushButton("Preview")
        lay_btns.addWidget(self.btn_connect); lay_btns.addWidget(self.btn_disconnect); lay_btns.addWidget(self.btn_preview)
        ctl_layout.addLayout(lay_btns)

        grp_stat = QtWidgets.QGroupBox("Status"); lay_stat = QtWidgets.QHBoxLayout()
        self.lbl_daq=QtWidgets.QLabel("DAQ:-"); self.lbl_g1=QtWidgets.QLabel("G1:-"); self.lbl_g2=QtWidgets.QLabel("G2:-"); self.lbl_g3=QtWidgets.QLabel("G3:-")
        lay_stat.addWidget(self.lbl_g1); lay_stat.addWidget(self.lbl_g2); lay_stat.addWidget(self.lbl_g3); lay_stat.addWidget(self.lbl_daq)
        grp_stat.setLayout(lay_stat); ctl_layout.addWidget(grp_stat)
        ctl_layout.addStretch(); ctl_widget.setMinimumWidth(380)

        # Right
        right_widget = QtWidgets.QWidget(); right_layout = QtWidgets.QVBoxLayout(right_widget)
        self.plot = PlotWidget(); self.plot.ax.set_xlabel("Fast Axis")
        self.btn_start = QtWidgets.QPushButton("START SWEEP"); self.btn_start.setMinimumHeight(40); self.btn_start.setStyleSheet("background-color:#e0f7fa")
        self.btn_stop = QtWidgets.QPushButton("STOP"); self.progress = QtWidgets.QProgressBar(); self.lbl_status = QtWidgets.QLabel("Idle")
        run_layout=QtWidgets.QHBoxLayout(); run_layout.addWidget(self.btn_start); run_layout.addWidget(self.btn_stop); run_layout.addWidget(self.progress)
        self.log = QtWidgets.QPlainTextEdit(); self.log.setReadOnly(True)
        right_layout.addWidget(self.plot, 2); right_layout.addLayout(run_layout); right_layout.addWidget(self.lbl_status); right_layout.addWidget(self.log, 1)
        main_layout.addWidget(scroll); main_layout.addWidget(right_widget)

        # Signals
        self.chk_link.stateChanged.connect(self.update_field_states)
        self.cbo_fast.currentIndexChanged.connect(self.on_fast_combo_changed) # Split handlers
        self.cbo_slow.currentIndexChanged.connect(self.on_slow_combo_changed)
        self.update_field_states()

    def _volt(self, sp, val): sp.setDecimals(3); sp.setRange(-20, 20); sp.setSingleStep(0.1); sp.setValue(val)
    
    def _wire(self):
        # 1. Connect the Buttons to their functions
        self.btn_connect.clicked.connect(self.on_connect)
        
        # --- THIS LINE MAKES THE DISCONNECT BUTTON WORK ---
        self.btn_disconnect.clicked.connect(self.on_disconnect_all) 
        
        self.btn_start.clicked.connect(self.start_run)
        self.btn_stop.clicked.connect(self.stop_run)
        self.btn_preview.clicked.connect(self.on_preview)
        
        # 2. Connect the "Linked" Logic
        self.chk_link.clicked.connect(self.update_field_states)
        
        # 3. Connect the Dropdowns (Comboboxes)
        # This one filters the Slow list when Fast changes
        self.cbo_fast.currentIndexChanged.connect(self.on_fast_combo_changed)
        # This one just updates the greyed-out boxes
        self.cbo_slow.currentIndexChanged.connect(self.update_field_states)

    # --- COMBO LOGIC ---
    # --- Logic to prevent Fast/Slow Collision ---
    def on_fast_combo_changed(self):
        # 1. Filter the Slow Combo options to prevent collision
        self._update_slow_combo_items_grid()
        # 2. Update the spinbox states (Grey out unused ones)
        self.update_field_states()

    def on_slow_combo_changed(self):
        if self._updating_combos: return
        # Changing Slow axis NEVER requires rebuilding lists (unless Linked check changes)
        # Just update enabled/disabled states
        self.update_field_states()

    def _update_slow_combo_items_grid(self):
        if self._updating_combos: return
        self._updating_combos = True
        
        fast = self.cbo_fast.currentText()
        current_slow = self.cbo_slow.currentText()
        
        # Disable signals so we don't trigger loops while rebuilding
        self.cbo_slow.blockSignals(True)
        self.cbo_slow.clear()
        
        # Always add 'None'
        self.cbo_slow.addItem("None")
        
        # Add axes ONLY if they are NOT the current Fast Axis
        all_axes = ["Vtg", "Vbg", "Vds"]
        for x in all_axes:
            if x != fast: 
                self.cbo_slow.addItem(x)
        
        # Restore selection if it's still valid (i.e. wasn't the one we just removed)
        # If the previous selection was the 'fast' axis, this will fail and we default to 0 (None)
        idx = self.cbo_slow.findText(current_slow)
        if idx >= 0:
            self.cbo_slow.setCurrentIndex(idx)
        else:
            self.cbo_slow.setCurrentIndex(0) # Default to None
            
        self.cbo_slow.blockSignals(False)
        self._updating_combos = False

    def update_field_states(self):
        if self._updating_combos: return
        self._updating_combos = True
        
        # In this new logic, the Checkbox just enables the Ratio box and 
        # changes the Plot X-axis to "Doping". It does NOT lock the inputs.
        use_ratio_calc = self.chk_link.isChecked()
        self.sp_ratio.setEnabled(use_ratio_calc)
        
        # Labels
        self.lbl_fast = "Fast Axis (Inner):"
        self.lbl_slow = "Slow Axis (Outer):"

        # Ensure Slow Axis is ENABLED (We need it for Megasweep)
        self.cbo_slow.setEnabled(True)
        
        # Ensure Fast/Slow logic prevents collision (Standard Grid Logic)
        active_sweep = [self.cbo_fast.currentText()]
        if self.cbo_slow.currentText() != "None":
            active_sweep.append(self.cbo_slow.currentText())

        # Enable/Disable rows based on selection
        vtg_active = ("Vtg" in active_sweep)
        self.sp_vtg_stop.setEnabled(vtg_active); self.sp_vtg_step.setEnabled(vtg_active)
        
        vbg_active = ("Vbg" in active_sweep)
        self.sp_vbg_stop.setEnabled(vbg_active); self.sp_vbg_step.setEnabled(vbg_active)
        
        vds_active = ("Vds" in active_sweep)
        self.sp_vds_stop.setEnabled(vds_active); self.sp_vds_step.setEnabled(vds_active)

        self._updating_combos = False
        self.on_axis_change_label()

    def on_axis_change_label(self):
        use_ratio = self.chk_link.isChecked()
        fast = self.cbo_fast.currentText()
        if use_ratio:
             r = self.sp_ratio.value()
             # Plot against calculated Doping
             self.plot.ax.set_xlabel(f"Calculated Doping ({r:.2f}*Vtg + Vbg)")
        else:
             # Standard raw voltage plot
             self.plot.ax.set_xlabel(f"{fast} (V)")
        self.plot.canvas.draw_idle()

    def on_preview(self):
        self.plot.ax.clear()
        use_ratio = self.chk_link.isChecked()
        fast_axis = self.cbo_fast.currentText()
        slow_axis = self.cbo_slow.currentText()
        
        # Helper to get range
        def get_p(name):
            if name == "Vtg": return self.sp_vtg_start.value(), self.sp_vtg_stop.value(), self.sp_vtg_step.value()
            if name == "Vbg": return self.sp_vbg_start.value(), self.sp_vbg_stop.value(), self.sp_vbg_step.value()
            if name == "Vds": return self.sp_vds_start.value(), self.sp_vds_stop.value(), self.sp_vds_step.value()
            return 0,0,1

        # Generate Fast Sequence
        f_start, f_stop, f_step = get_p(fast_axis)
        f_step = abs(f_step) * (1 if f_stop >= f_start else -1)
        if abs(f_step) < 1e-9: f_seq = [f_start]
        else: f_seq = _frange_inc(f_start, f_stop, f_step)

        # Generate Slow Sequence
        s_seq = [0.0]
        if slow_axis != "None":
            s_start, s_stop, s_step = get_p(slow_axis)
            s_step = abs(s_step) * (1 if s_stop >= s_start else -1)
            if abs(s_step) < 1e-9: s_seq = [s_start]
            else: s_seq = _frange_inc(s_start, s_stop, s_step)

        xs = []; ys = []
        
        # Simulate Megasweep
        for s_val in s_seq:
            for f_val in f_seq:
                # Map values
                curr_vtg = f_val if fast_axis=="Vtg" else (s_val if slow_axis=="Vtg" else self.sp_vtg_start.value())
                curr_vbg = f_val if fast_axis=="Vbg" else (s_val if slow_axis=="Vbg" else self.sp_vbg_start.value())
                
                if use_ratio:
                    # Plot Doping vs Efield (or Doping vs Index)
                    ratio = self.sp_ratio.value()
                    doping = ratio * curr_vtg + curr_vbg
                    efield = ratio * curr_vtg - curr_vbg
                    xs.append(doping)
                    ys.append(efield)
                else:
                    # Standard Grid Preview
                    xs.append(f_val)
                    ys.append(s_val)

        if use_ratio:
            self.plot.ax.scatter(xs, ys, s=15, c='blue', alpha=0.5)
            self.plot.ax.set_xlabel("Doping (Ratio*Vtg + Vbg)")
            self.plot.ax.set_ylabel("Efield (Ratio*Vtg - Vbg)")
            self.plot.ax.set_title(f"Megasweep Area (Ratio={self.sp_ratio.value()}): {len(xs)} pts")
        else:
            self.plot.ax.scatter(xs, ys, s=15, c='blue')
            self.plot.ax.set_xlabel(f"{fast_axis} (V)")
            self.plot.ax.set_ylabel(f"{slow_axis if slow_axis!='None' else 'Fixed'} (V)")
            self.plot.ax.set_title(f"Standard Grid: {len(xs)} pts")

        self.plot.ax.grid(True); self.plot.canvas.draw_idle()

    def on_axis_change_label(self):
        is_linked = self.chk_link.isChecked()
        fast = self.cbo_fast.currentText()
        if is_linked:
             other = "Vbg" if fast=="Vtg" else "Vtg"
             r = self.sp_ratio.value()
             self.plot.ax.set_xlabel(f"Doping (r={r:.2f}*{fast} + {other})")
        else:
             self.plot.ax.set_xlabel(f"{fast} (V)")
        self.plot.canvas.draw_idle()

    # --- Boilerplate ---
    def on_set_generic(self, name):
        val = 0.0
        if name=="Vtg": val=self.sp_vtg_start.value(); sess=self.s_g1
        elif name=="Vbg": val=self.sp_vbg_start.value(); sess=self.s_g2
        elif name=="Vds": val=self.sp_vds_start.value(); sess=self.s_daq if "NI DAQ" in self.cbo_source.currentText() else self.s_g3
        if sess: 
            try: sess.ramp_voltage(val, 0.5); self.log.appendPlainText(f"Set {name} -> {val}")
            except Exception as e: self.log.appendPlainText(str(e))
        else: self.log.appendPlainText(f"{name} not connected")

    def collect_params(self):
        self.p.base_name = self.ed_base.text()
        src = self.cbo_source.currentText()
        if "NI DAQ" in src: self.p.vds_source="NI DAQ AO"; self.p.ao_channel=int(src.split()[-1].replace('ao',''))
        else: self.p.vds_source = "Keithley 2400"
        self.p.vtg_start=self.sp_vtg_start.value(); self.p.vtg_stop=self.sp_vtg_stop.value(); self.p.vtg_step=self.sp_vtg_step.value()
        self.p.vbg_start=self.sp_vbg_start.value(); self.p.vbg_stop=self.sp_vbg_stop.value(); self.p.vbg_step=self.sp_vbg_step.value()
        self.p.vds_start=self.sp_vds_start.value(); self.p.vds_stop=self.sp_vds_stop.value(); self.p.vds_step=self.sp_vds_step.value()
        self.p.mode = "Linked" if self.chk_link.isChecked() else "Grid"
        self.p.axis_fast = self.cbo_fast.currentText()
        self.p.axis_slow = self.cbo_slow.currentText()
        self.p.ratio = self.sp_ratio.value()
        self.p.delay = self.sp_delay.value(); self.p.n_sample=self.sp_nsamp.value(); self.p.plot_choice=self.cbo_y.currentText()

    def _ensure_sessions(self) -> bool:
        mw = self.window()
        if hasattr(mw, 'refresh_models_from_ui'): mw.refresh_models_from_ui()
        self.log.appendPlainText(f"Addresses -> G1:{self.conns.gate1}  G2:{self.conns.gate2}  G3:{self.conns.gate3}  DAQ:{self.conns.daq_dev}")
        
        # --- Connect Gate 1 ---
        if self.s_g1 is None:
            try:
                if not self.conns.gate1: raise ValueError("Address empty")
                self.s_g1 = Keithley2400VoltMode('g1', self.conns.gate1, curr_comp=1e-7, volt_comp=40)
                self.s_g1.connect()
                # FIX: Include "G1:" in the text
                self.lbl_g1.setText("G1: OK"); self.lbl_g1.setStyleSheet("color: green; font-weight: bold")
            except Exception as ex:
                self.lbl_g1.setText("G1: Err"); self.lbl_g1.setStyleSheet("color: red")
                self.log.appendPlainText(f"Gate 1 failed: {ex}"); self.s_g1 = None

        # --- Connect Gate 2 ---
        if self.s_g2 is None:
            try:
                if not self.conns.gate2: raise ValueError("Address empty")
                self.s_g2 = Keithley2400VoltMode('g2', self.conns.gate2, curr_comp=1e-7, volt_comp=40)
                self.s_g2.connect()
                # FIX: Include "G2:" in the text
                self.lbl_g2.setText("G2: OK"); self.lbl_g2.setStyleSheet("color: green; font-weight: bold")
            except Exception as ex:
                self.lbl_g2.setText("G2: Err"); self.lbl_g2.setStyleSheet("color: red")
                self.log.appendPlainText(f"Gate 2 failed: {ex}"); self.s_g2 = None

        # --- Connect Gate 3 (Vds) ---
        if self.s_g3 is None:
            try:
                if not self.conns.gate3: raise ValueError("Address empty")
                self.s_g3 = Keithley2400VoltMode('g3', self.conns.gate3, curr_comp=1e-6, volt_comp=20)
                self.s_g3.connect()
                # FIX: Include "G3:" in the text
                self.lbl_g3.setText("G3: OK"); self.lbl_g3.setStyleSheet("color: green; font-weight: bold")
            except Exception as ex:
                self.lbl_g3.setText("G3: Err"); self.lbl_g3.setStyleSheet("color: red")

        # --- Connect DAQ (Required) ---
        try:
            if self.s_daq is None:
                ao_items = self.get_ao_items()
                ao_indexes = [int(i[2:]) for i in ao_items if i.startswith("ao")]
                
                self.s_daq = DaqCard(address=self.conns.daq_dev, ao_channel_indexes=ao_indexes, ai_channel_indexes=[0,1,2,3], read_delay=0.5)
                self.s_daq.connect()
                # FIX: Include "DAQ:" in the text
                self.lbl_daq.setText("DAQ: OK"); self.lbl_daq.setStyleSheet("color: green; font-weight: bold")
                
                # Update the Vds Source ComboBox
                items = ["Keithley 2400"] + [f"NI DAQ {a}" for a in ao_items]
                self.cbo_source.blockSignals(True)
                cur = self.cbo_source.currentText()
                self.cbo_source.clear()
                self.cbo_source.addItems(items)
                if cur in items: self.cbo_source.setCurrentText(cur)
                self.cbo_source.blockSignals(False)
        except Exception as ex:
            QtWidgets.QMessageBox.critical(self, "Connect error", str(ex)); return False
            
        return True

    def on_connect(self):
        if self._ensure_sessions(): QtWidgets.QMessageBox.information(self, "Connect", "Connected.")

    def on_disconnect_all(self):
        # 1. Loop through all devices and their labels
        #    (Gate1, Gate2, Gate3, DAQ)
        for obj, lab, prefix in [
            (self.s_g1, self.lbl_g1, "G1"), 
            (self.s_g2, self.lbl_g2, "G2"), 
            (self.s_g3, self.lbl_g3, "G3"), 
            (self.s_daq, self.lbl_daq, "DAQ")
        ]:
            # Safe disconnect (ignores if already None)
            _safe(obj, 'disconnect')
            _safe(obj, 'close')
            
            # Reset label to gray dash with device name
            lab.setText(f"{prefix}: -")
            lab.setStyleSheet("color: black;") 

        # 2. Clear the session variables so we know they are gone
        self.s_g1 = None
        self.s_g2 = None
        self.s_g3 = None
        self.s_daq = None
        
        self.log.appendPlainText("All sessions disconnected.")

    def start_run(self):
        if self.worker_thread: return
        self.collect_params(); self.plot.clear(); self.on_axis_change_label()
        try:
            amp, lkn = self.window().conn_dock.get_rates()
            self.worker = CoWorker(self.p, self.save, self.conns, g1=self.s_g1, g2=self.s_g2, g3=self.s_g3, daq=self.s_daq, plot_choice=self.p.plot_choice, amp_rate=amp, lkn_rate=lkn)
            self.worker_thread = QtCore.QThread(); self.worker.moveToThread(self.worker_thread)
            self.worker_thread.started.connect(self.worker.run)
            self.worker.point.connect(self.on_point); self.worker.clear_plot.connect(self._clear_plot)
            self.worker.progress.connect(lambda p: self.progress.setValue(int(p*100)))
            self.worker.status.connect(self._update_status); self.worker.log.connect(self.log.appendPlainText)
            self.worker.finished.connect(self.on_finished); self.worker.finished.connect(self.worker_thread.quit)
            self.worker.error.connect(self.on_error); self.worker.error.connect(self.worker_thread.quit)
            self.worker_thread.finished.connect(self._cleanup_thread); self.worker_thread.start()
        except: pass
    def stop_run(self): 
        if self.worker: self.worker.request_stop()
    def _update_status(self, m): self.lbl_status.setText(m)
    def _clear_plot(self): self.plot.clear(); self.on_axis_change_label()
    def on_point(self, x, y):
        ax=self.plot.ax; ln=ax.lines[0] if ax.lines else ax.plot([],[],'o-')[0]
        ln.set_data(list(ln.get_xdata())+[x], list(ln.get_ydata())+[y])
        ax.relim(); ax.autoscale_view(); self.plot.canvas.draw_idle()
    def _cleanup_thread(self): 
        if self.worker: self.worker.deleteLater(); self.worker=None; self.worker_thread=None
    def on_error(self, m): self.lbl_status.setText(f"Err: {m}")
    def on_finished(self, p): self.lbl_status.setText("Finished")


# ----------------- Photocurrent (wavelength scan) -----------------
class PhotocurrentParams(QtCore.QObject):
    def __init__(self):
        super().__init__()
        self.base_name = "pcspec"
        self.use_vds = False
        self.vds_source = "None"  # None, Keithley 2400, NI DAQ aoX
        self.ao_channel = 0
        self.vds_set = 0.0
        self.vds_ramp = 0.01
        self.vtg_set = 0.0
        self.vbg_set = 0.0
        self.vg_ramp = 0.2
        self.wl_start = 550.0
        self.wl_stop  = 740.0
        self.wl_step  = 0.5
        self.delay = 0.01
        self.n_sample = 1
        self.plot_choice = "Ids_DC"

class PhotocurrentWorker(RunWorker):
    def __init__(self, params: PhotocurrentParams, save: SaveRoot, conns: Connections, **kw):
        super().__init__(); self.p=params; self.save=save; self.conns=conns
        self.g1=kw.get('g1'); self.g2=kw.get('g2'); self.g3=kw.get('g3'); self.daq=kw.get('daq'); self.mono=kw.get('mono')
        self.plot_choice=kw.get('plot_choice'); self.amp_rate=kw.get('amp_rate',1e7); self.lkn_rate=kw.get('lkn_rate',100.0)

    @QtCore.pyqtSlot()
    def run(self):
        try:
            # --- MODIFIED: Relaxed checks ---
            # We strictly need DAQ (to read) and Mono (to change wavelength).
            if self.daq is None or self.mono is None:
                self.error.emit("Required sessions missing: DAQ or Monochromator"); return
            # We don't enforce G1/G2/Vds presence anymore. 
            # --------------------------------

            for f in ("vds_set","vtg_set","vbg_set"):
                setattr(self.p, f, clamp(getattr(self.p,f), -V_LIMIT, V_LIMIT))

            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            tag_vds = "novds"
            if self.p.use_vds:
                if self.p.vds_source.startswith("NI DAQ"):
                    tag_vds = f"ao{self.p.ao_channel}"
                elif self.p.vds_source == "Keithley 2400":
                    tag_vds = "keth"
            
            # Tag filename with active gates for clarity
            g1_tag = "Tg" if self.g1 else "NoTg"
            g2_tag = "Bg" if self.g2 else "NoBg"

            stem = f"{_sanitize_base(self.p.base_name)}_{g1_tag}_{g2_tag}_pc_{tag_vds}_Vtg{self.p.vtg_set:+.3f}V_Vbg{self.p.vbg_set:+.3f}V_{ts}"
            out_dir = self.save.path()
            csv_path = os.path.join(out_dir, stem + ".csv")
            self.log.emit(f"Save -> {csv_path}")

            need_header = not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0
            with open(csv_path, 'a', newline='', buffering=1, encoding='utf-8') as f:
                w = csv.writer(f)
                if need_header:
                    w.writerow(['Wavelength','Vg1','Vg2','Vds','raw_X','raw_Y','raw_DC','Ids_X','Ids_Y','Ids_DC'])
                    w.writerow(['nm','V','V','V','A','A','A','A','A','A'])
                    self.log.emit("[csv] header written")

                # _safe handles None objects silently
                _safe(self.g1, 'ramp_voltage', self.p.vtg_set, self.p.vg_ramp)
                _safe(self.g2, 'ramp_voltage', self.p.vbg_set, self.p.vg_ramp)

                vds_now = 0.0
                if self.p.use_vds:
                    vds_now = float(self.p.vds_set)
                    if self.p.vds_source == "Keithley 2400":
                        if self.g3:
                            _safe(self.g3, 'ramp_voltage', vds_now, self.p.vds_ramp)
                        else:
                            self.error.emit("Vds Source set to Keithley but Gate3 not connected."); return
                    elif self.p.vds_source.startswith("NI DAQ"):
                        _safe(self.daq, 'ramp_voltage', self.p.ao_channel, vds_now, self.p.vds_ramp)

                wl = self.p.wl_start; step = self.p.wl_step if self.p.wl_stop >= self.p.wl_start else -abs(self.p.wl_step)
                total = max(1, int(1 + round((self.p.wl_stop - self.p.wl_start) / (step if step != 0 else 1e-9))))
                idx = 0
                while True:
                    self.check_abort_pause()
                    _safe(self.mono, 'set_wavelength', float(wl))
                    time.sleep(max(0.0, self.p.delay))

                    raw_X=raw_Y=raw_DC=0.0
                    for _ in range(int(self.p.n_sample)):
                        self.check_abort_pause()
                        self.daq.acquire()
                        raw_X += self.daq.get_ai_value(0)
                        raw_Y += self.daq.get_ai_value(1)
                        raw_DC += self.daq.get_ai_value(2)
                    raw_X/=self.p.n_sample; raw_Y/=self.p.n_sample; raw_DC/=self.p.n_sample

                    ids_X = raw_X/(self.amp_rate*self.lkn_rate)
                    ids_Y = raw_Y/(self.amp_rate*self.lkn_rate)
                    ids_DC= raw_DC/self.amp_rate

                    w.writerow([float(wl), self.p.vtg_set, self.p.vbg_set, vds_now, raw_X, raw_Y, raw_DC, ids_X, ids_Y, ids_DC])
                    try: f.flush(); os.fsync(f.fileno())
                    except Exception: pass

                    y = ids_DC if self.plot_choice=='Ids_DC' else (ids_X if self.plot_choice=='Ids_X' else ids_Y)
                    self.point.emit(float(wl), y)
                    idx += 1; self.progress.emit(idx/total)
                    if (step >= 0 and wl >= self.p.wl_stop - 1e-12) or (step < 0 and wl <= self.p.wl_stop + 1e-12):
                        break
                    wl += step

            self.finished.emit(csv_path)
        except Exception as ex:
            self.error.emit(str(ex))
        finally:
            try:
                _safe(self.g1, 'ramp_voltage', 0.0, self.p.vg_ramp)
                _safe(self.g2, 'ramp_voltage', 0.0, self.p.vg_ramp)
                if self.p.use_vds:
                    if self.p.vds_source == "Keithley 2400": _safe(self.g3, 'ramp_voltage', 0.0, self.p.vds_ramp)
                    elif self.p.vds_source.startswith("NI DAQ"): _safe(self.daq, 'ramp_voltage', self.p.ao_channel, 0.0, self.p.vds_ramp)
            except Exception: pass
            self.log.emit("Outputs returned to 0 V; sessions kept open.")

class PhotocurrentTab(QtWidgets.QWidget):
    def __init__(self, save: SaveRoot, conns: Connections, get_global_rates_callable=None, get_ao_items_callable=None):
        super().__init__()
        self.save = save; self.conns = conns
        self.get_global_rates = get_global_rates_callable or (lambda: (1e7,100.0))
        self.get_ao_items = get_ao_items_callable or (lambda: ["ao0","ao1"])
        self.p = PhotocurrentParams()
        self.s_g1=self.s_g2=self.s_g3=self.s_daq=self.s_mono=None
        self.worker_thread=None; self.worker=None
        self._build_ui(); self._wire()

    def _build_ui(self):
        main_layout = QtWidgets.QHBoxLayout(self)

        # Scroll
        scroll = QtWidgets.QScrollArea(); scroll.setWidgetResizable(True)
        ctl_widget = QtWidgets.QWidget(); scroll.setWidget(ctl_widget)
        ctl_layout = QtWidgets.QVBoxLayout(ctl_widget)
        ctl_layout.setSpacing(5); ctl_layout.setContentsMargins(5,5,5,5)

        # --- 1. Config (Vertical Layout to match pic) ---
        grp_conf = QtWidgets.QGroupBox("Configuration")
        form_conf = QtWidgets.QFormLayout() # <--- Changed to QFormLayout
        form_conf.setContentsMargins(5,5,5,5)
        
        self.ed_base = QtWidgets.QLineEdit(self.p.base_name)
        self.cbo_source = QtWidgets.QComboBox(); self.cbo_source.addItems(["None","Keithley 2400"]) 
        self.cbo_y = QtWidgets.QComboBox(); self.cbo_y.addItems(["Ids_DC","Ids_X","Ids_Y"])
        
        form_conf.addRow("Filename:", self.ed_base)
        form_conf.addRow("Vds Source:", self.cbo_source)
        form_conf.addRow("Plot Axis:", self.cbo_y)
        
        grp_conf.setLayout(form_conf)
        ctl_layout.addWidget(grp_conf)

        # 2. Vds Bias
        grp_vds = QtWidgets.QGroupBox("Vds Bias")
        lay_vds = QtWidgets.QHBoxLayout()
        self.chk_use_vds = QtWidgets.QCheckBox("Enable"); self.chk_use_vds.setChecked(False)
        self.sp_vds = QtWidgets.QDoubleSpinBox(); self._volt(self.sp_vds, 0.0)
        self.sp_vds_ramp = QtWidgets.QDoubleSpinBox(); self.sp_vds_ramp.setDecimals(3); self.sp_vds_ramp.setValue(0.01)
        
        lay_vds.addWidget(self.chk_use_vds)
        lay_vds.addWidget(QtWidgets.QLabel("Set(V):")); lay_vds.addWidget(self.sp_vds)
        btn_set_vds = QtWidgets.QPushButton("Set"); btn_set_vds.setFixedWidth(40); btn_set_vds.clicked.connect(self.on_set_vds)
        lay_vds.addWidget(btn_set_vds)
        lay_vds.addWidget(QtWidgets.QLabel("Ramp:")); lay_vds.addWidget(self.sp_vds_ramp)
        grp_vds.setLayout(lay_vds)
        ctl_layout.addWidget(grp_vds)

        # 3. Gate Fixed
        grp_gate = QtWidgets.QGroupBox("Fixed Gates")
        lay_gate = QtWidgets.QHBoxLayout()
        self.sp_vtg = QtWidgets.QDoubleSpinBox(); self._volt(self.sp_vtg, 0.0)
        self.sp_vbg = QtWidgets.QDoubleSpinBox(); self._volt(self.sp_vbg, 0.0)
        self.sp_vg_ramp = QtWidgets.QDoubleSpinBox(); self.sp_vg_ramp.setDecimals(3); self.sp_vg_ramp.setValue(0.2)
        
        lay_gate.addWidget(QtWidgets.QLabel("Vtg:")); lay_gate.addWidget(self.sp_vtg)
        b1 = QtWidgets.QPushButton("Set"); b1.setFixedWidth(30); b1.clicked.connect(self.on_set_vtg); lay_gate.addWidget(b1)
        lay_gate.addSpacing(10)
        lay_gate.addWidget(QtWidgets.QLabel("Vbg:")); lay_gate.addWidget(self.sp_vbg)
        b2 = QtWidgets.QPushButton("Set"); b2.setFixedWidth(30); b2.clicked.connect(self.on_set_vbg); lay_gate.addWidget(b2)
        grp_gate.setLayout(lay_gate)
        ctl_layout.addWidget(grp_gate)

        # 4. Wavelength
        grp_wl = QtWidgets.QGroupBox("Wavelength Scan (nm)")
        lay_wl = QtWidgets.QHBoxLayout()
        self.sp_wls = QtWidgets.QDoubleSpinBox(); self.sp_wls.setRange(200,2000); self.sp_wls.setValue(550)
        self.sp_wle = QtWidgets.QDoubleSpinBox(); self.sp_wle.setRange(200,2000); self.sp_wle.setValue(740)
        self.sp_wld = QtWidgets.QDoubleSpinBox(); self.sp_wld.setRange(0.001,100); self.sp_wld.setValue(0.5)
        
        lay_wl.addWidget(QtWidgets.QLabel("Start:")); lay_wl.addWidget(self.sp_wls)
        lay_wl.addWidget(QtWidgets.QLabel("Stop:")); lay_wl.addWidget(self.sp_wle)
        lay_wl.addWidget(QtWidgets.QLabel("Step:")); lay_wl.addWidget(self.sp_wld)
        btn_go = QtWidgets.QPushButton("Go"); btn_go.setFixedWidth(35); btn_go.clicked.connect(self.on_go_wl)
        lay_wl.addWidget(btn_go)
        grp_wl.setLayout(lay_wl)
        ctl_layout.addWidget(grp_wl)

        # 5. Timing
        grp_time = QtWidgets.QGroupBox("Timing Control")
        lay_time = QtWidgets.QHBoxLayout()
        self.sp_delay = QtWidgets.QDoubleSpinBox(); self.sp_delay.setValue(0.01)
        self.sp_nsamp = QtWidgets.QSpinBox(); self.sp_nsamp.setValue(1)
        
        lay_time.addWidget(QtWidgets.QLabel("Delay(s):")); lay_time.addWidget(self.sp_delay)
        lay_time.addWidget(QtWidgets.QLabel("Avg:")); lay_time.addWidget(self.sp_nsamp)
        grp_time.setLayout(lay_time)
        ctl_layout.addWidget(grp_time)
        lay_btns = QtWidgets.QHBoxLayout()
        self.btn_connect = QtWidgets.QPushButton("Connect")
        self.btn_disconnect = QtWidgets.QPushButton("Disconnect")
        
        # Optional: Make them slightly taller/easier to click
        self.btn_connect.setMinimumHeight(30)
        self.btn_disconnect.setMinimumHeight(30)
        
        lay_btns.addWidget(self.btn_connect)
        lay_btns.addWidget(self.btn_disconnect)
        ctl_layout.addLayout(lay_btns)   

        # Status
        grp_stat = QtWidgets.QGroupBox("Status")
        lay_stat = QtWidgets.QHBoxLayout()

        self.lbl_g1 = QtWidgets.QLabel("G1: -")
        self.lbl_g2 = QtWidgets.QLabel("G2: -")
        self.lbl_g3 = QtWidgets.QLabel("G3: -")
        self.lbl_daq = QtWidgets.QLabel("DAQ: -")
        self.lbl_mono = QtWidgets.QLabel("Mono: -")
        for l in [self.lbl_g1, self.lbl_g2, self.lbl_g3, self.lbl_daq, self.lbl_mono]:
            lay_stat.addWidget(l)
        grp_stat.setLayout(lay_stat)
        ctl_layout.addWidget(grp_stat)

        ctl_layout.addStretch()
        ctl_widget.setMinimumWidth(400)

        # Right Col
        right_widget = QtWidgets.QWidget(); right_layout = QtWidgets.QVBoxLayout(right_widget)
        self.plot = PlotWidget(); self.plot.ax.set_xlabel("Wavelength (nm)")
        self.btn_start = QtWidgets.QPushButton("START PHOTOCURRENT"); self.btn_start.setMinimumHeight(40); self.btn_start.setStyleSheet("background-color: #e0f7fa")
        self.btn_stop = QtWidgets.QPushButton("STOP")
        self.progress = QtWidgets.QProgressBar()
        self.lbl_status = QtWidgets.QLabel("Idle")
        
        run_layout = QtWidgets.QHBoxLayout(); run_layout.addWidget(self.btn_start); run_layout.addWidget(self.btn_stop); run_layout.addWidget(self.progress)
        self.log = QtWidgets.QPlainTextEdit(); self.log.setReadOnly(True)

        right_layout.addWidget(self.plot, 2); right_layout.addLayout(run_layout)
        right_layout.addWidget(self.lbl_status); right_layout.addWidget(self.log, 1)
        main_layout.addWidget(scroll); main_layout.addWidget(right_widget)

    def _volt(self, sp, val): sp.setDecimals(3); sp.setRange(-V_LIMIT, V_LIMIT); sp.setSingleStep(0.1); sp.setValue(val)
    def _wire(self):
        self.btn_connect.clicked.connect(self.on_connect)
        self.btn_disconnect.clicked.connect(self.on_disconnect_all)
        self.btn_start.clicked.connect(self.start_run)
        self.btn_stop.clicked.connect(self.stop_run)
    # Slots
    def on_set_vtg(self):
        if self.s_g1: self.s_g1.ramp_voltage(self.sp_vtg.value(), 0.5)
    def on_set_vbg(self):
        if self.s_g2: self.s_g2.ramp_voltage(self.sp_vbg.value(), 0.5)
    def on_set_vds(self):
        src = self.cbo_source.currentText(); val = self.sp_vds.value()
        if src == "Keithley 2400" and self.s_g3: self.s_g3.ramp_voltage(val, 0.5)
        elif "NI DAQ" in src and self.s_daq: self.s_daq.ramp_voltage(int(src.split()[-1].replace('ao','')), val, 0.5)
    def on_go_wl(self):
        if self.s_mono: self.s_mono.set_wavelength(self.sp_wls.value())

    # Connection Logic (Compact)
    def _ensure_sessions(self) -> bool:
        mw = self.window()
        if hasattr(mw, 'refresh_models_from_ui'): mw.refresh_models_from_ui()
        
        # Clean DAQ name
        if self.conns.daq_dev: self.conns.daq_dev = self.conns.daq_dev.strip()
        self.log.appendPlainText(f"Addresses -> Mono:{self.conns.mono} DAQ:{self.conns.daq_dev}")

        # --- Connect Gate 1 ---
        if self.s_g1 is None and self.conns.gate1:
            try: 
                self.s_g1 = Keithley2400VoltMode('g1', self.conns.gate1)
                self.s_g1.connect()
                self.lbl_g1.setText("G1: OK"); self.lbl_g1.setStyleSheet("color: green; font-weight: bold")
            except Exception as e: 
                self.lbl_g1.setText("G1: Err"); self.lbl_g1.setStyleSheet("color: red")
                self.log.appendPlainText(f"G1 failed: {e}")

        # --- Connect Gate 2 ---
        if self.s_g2 is None and self.conns.gate2:
            try: 
                self.s_g2 = Keithley2400VoltMode('g2', self.conns.gate2)
                self.s_g2.connect()
                self.lbl_g2.setText("G2: OK"); self.lbl_g2.setStyleSheet("color: green; font-weight: bold")
            except Exception as e: 
                self.lbl_g2.setText("G2: Err"); self.lbl_g2.setStyleSheet("color: red")
                self.log.appendPlainText(f"G2 failed: {e}")

        # --- Connect Gate 3 ---
        if self.s_g3 is None and self.conns.gate3:
            try: 
                self.s_g3 = Keithley2400VoltMode('g3', self.conns.gate3)
                self.s_g3.connect()
                self.lbl_g3.setText("G3: OK"); self.lbl_g3.setStyleSheet("color: green; font-weight: bold")
            except Exception as e: 
                self.lbl_g3.setText("G3: Err"); self.lbl_g3.setStyleSheet("color: red")
                self.log.appendPlainText(f"G3 failed: {e}")

        # --- Connect Monochromator (REQUIRED) ---
        if self.s_mono is None:
            try:
                self.s_mono = SP2300('m', self.conns.mono)
                self.s_mono.connect()
                self.lbl_mono.setText("Mono: OK"); self.lbl_mono.setStyleSheet("color: green; font-weight: bold")
            except Exception as e:
                self.lbl_mono.setText("Mono: Err"); self.lbl_mono.setStyleSheet("color: red")
                self.log.appendPlainText(f"Mono failed: {e}")
                self.s_mono = None 

        # --- Connect DAQ (REQUIRED) ---
        if self.s_daq is None:
            try:
                ao_items = self.get_ao_items()
                ao_idx = [int(i[2:]) for i in ao_items if i.startswith("ao")]
                self.s_daq = DaqCard(address=self.conns.daq_dev, ao_channel_indexes=ao_idx, ai_channel_indexes=[0,1,2,3], read_delay=0.5)
                self.s_daq.connect()
                self.lbl_daq.setText("DAQ: OK"); self.lbl_daq.setStyleSheet("color: green; font-weight: bold")
                
                # Update Source dropdown
                items = ["None", "Keithley 2400"] + [f"NI DAQ {a}" for a in ao_items]
                self.cbo_source.blockSignals(True)
                cur = self.cbo_source.currentText()
                self.cbo_source.clear()
                self.cbo_source.addItems(items)
                if cur in items: self.cbo_source.setCurrentText(cur)
                self.cbo_source.blockSignals(False)
            except Exception as e:
                self.lbl_daq.setText("DAQ: Err"); self.lbl_daq.setStyleSheet("color: red")
                self.log.appendPlainText(f"DAQ failed: {e}")
                self.s_daq = None

        # Return True only if critical devices are ready
        return (self.s_mono is not None and self.s_daq is not None)


    def on_disconnect_all(self):
        # Disconnect all objects
        for o in [self.s_g1, self.s_g2, self.s_g3, self.s_daq, self.s_mono]:
            _safe(o, 'disconnect')
            _safe(o, 'close')
        
        self.s_g1 = self.s_g2 = self.s_g3 = self.s_daq = self.s_mono = None
        
        # Reset labels
        for lbl, prefix in [
            (self.lbl_g1, "G1"), (self.lbl_g2, "G2"), (self.lbl_g3, "G3"), 
            (self.lbl_daq, "DAQ"), (self.lbl_mono, "Mono")
        ]:
            lbl.setText(f"{prefix}: -")
            lbl.setStyleSheet("color: black;")
            
        self.log.appendPlainText("Disconnected all devices.")

    def on_connect(self):
        if self._ensure_sessions():
            QtWidgets.QMessageBox.information(self, "Connect", "Connected.")
    # Run logic (Standard)
    def collect_params(self):
        self.p.base_name = self.ed_base.text(); self.p.use_vds = self.chk_use_vds.isChecked()
        src = self.cbo_source.currentText()
        if "NI DAQ" in src: self.p.vds_source="NI DAQ AO"; self.p.ao_channel=int(src.split()[-1].replace('ao',''))
        else: self.p.vds_source = src
        self.p.vds_set=self.sp_vds.value(); self.p.vtg_set=self.sp_vtg.value(); self.p.vbg_set=self.sp_vbg.value()
        self.p.wl_start=self.sp_wls.value(); self.p.wl_stop=self.sp_wle.value(); self.p.wl_step=self.sp_wld.value()
        self.p.delay=self.sp_delay.value(); self.p.n_sample=self.sp_nsamp.value(); self.p.plot_choice=self.cbo_y.currentText()

    def start_run(self):
        if self.worker_thread: return
        if not self._ensure_sessions(): return
        self.collect_params(); self.plot.clear()
        try:
            amp, lkn = self.window().conn_dock.get_rates()
            self.worker = PhotocurrentWorker(self.p, self.save, self.conns, g1=self.s_g1, g2=self.s_g2, g3=self.s_g3, daq=self.s_daq, mono=self.s_mono, plot_choice=self.p.plot_choice, amp_rate=amp, lkn_rate=lkn)
            self.worker_thread = QtCore.QThread(); self.worker.moveToThread(self.worker_thread)
            self.worker_thread.started.connect(self.worker.run)
            self.worker.point.connect(self.on_point); self.worker.progress.connect(lambda p: self.progress.setValue(int(p*100)))
            self.worker.status.connect(self.lbl_status.setText); self.worker.log.connect(self.log.appendPlainText)
            self.worker.finished.connect(self.on_finished); self.worker.finished.connect(self.worker_thread.quit)
            self.worker.error.connect(self.on_error); self.worker.error.connect(self.worker_thread.quit)
            self.worker_thread.finished.connect(self._cleanup_thread); self.worker_thread.start()
        except Exception as e: self.log.appendPlainText(str(e))

    def stop_run(self): 
        if self.worker: self.worker.request_stop()
    def _cleanup_thread(self): 
        if self.worker: self.worker.deleteLater(); self.worker=None; self.worker_thread=None
    def on_point(self, x, y):
        ax=self.plot.ax; ln=ax.lines[0] if ax.lines else ax.plot([],[],'o')[0]
        ln.set_data(list(ln.get_xdata())+[x], list(ln.get_ydata())+[y])
        ax.relim(); ax.autoscale_view(); self.plot.canvas.draw_idle()
    def on_finished(self, p): self.lbl_status.setText("Finished"); self.log.appendPlainText(f"Saved: {p}")
    def on_error(self, m): self.lbl_status.setText(f"Err: {m}")

# ----------------- Connection panel -----------------
class ConnDock(QtWidgets.QWidget):
    stop_requested = QtCore.pyqtSignal() # Signal to main window

    def __init__(self):
        super().__init__()
        self.conns = Connections()
        self.save_root = SaveRoot()
        self._build()

    def _build(self):
        layout = QtWidgets.QVBoxLayout(self)

        # --- Group 1: Hardware Addresses ---
        grp_hw = QtWidgets.QGroupBox("Hardware Addresses")
        form_hw = QtWidgets.QFormLayout()
        self.ed_g1 = QtWidgets.QLineEdit(self.conns.gate1)
        self.ed_g2 = QtWidgets.QLineEdit(self.conns.gate2)
        self.ed_g3 = QtWidgets.QLineEdit(self.conns.gate3)
        self.ed_daq = QtWidgets.QLineEdit(self.conns.daq_dev)
        self.ed_mono = QtWidgets.QLineEdit(self.conns.mono)
        form_hw.addRow("Gate1/Vtg:", self.ed_g1)
        form_hw.addRow("Gate2/Vbg:", self.ed_g2)
        form_hw.addRow("G3/Vds:", self.ed_g3)
        form_hw.addRow("DAQ:", self.ed_daq)
        form_hw.addRow("Mono:", self.ed_mono)
        grp_hw.setLayout(form_hw)
        layout.addWidget(grp_hw)

        # --- Group 2: Save Settings ---
        grp_save = QtWidgets.QGroupBox("Save Settings")
        form_save = QtWidgets.QFormLayout()
        self.ed_user = QtWidgets.QLineEdit(self.save_root.user)
        self.ed_sample = QtWidgets.QLineEdit(self.save_root.sample)
        self.ed_base = QtWidgets.QLineEdit(self.save_root.base)
        form_save.addRow("User:", self.ed_user)
        form_save.addRow("Sample:", self.ed_sample)
        form_save.addRow("Base:", self.ed_base)
        grp_save.setLayout(form_save)
        layout.addWidget(grp_save)

        # --- Group 3: Global Rates ---
        grp_rate = QtWidgets.QGroupBox("Global Rates")
        form_rate = QtWidgets.QFormLayout()
        self.sp_amp = QtWidgets.QDoubleSpinBox(); self.sp_amp.setDecimals(2); self.sp_amp.setRange(1,1e12); self.sp_amp.setValue(1e7)
        self.sp_lkn = QtWidgets.QDoubleSpinBox(); self.sp_lkn.setDecimals(2); self.sp_lkn.setRange(0.001,1e6); self.sp_lkn.setValue(100.0)
        form_rate.addRow("Amp (V/A):", self.sp_amp)
        form_rate.addRow("Lkn Gain:", self.sp_lkn)
        grp_rate.setLayout(form_rate)
        layout.addWidget(grp_rate)

        # --- Emergency Stop ---
        self.btn_stop = QtWidgets.QPushButton("STOP / ZERO ALL")
        self.btn_stop.setMinimumHeight(50)
        self.btn_stop.setStyleSheet("background-color: red; color: white; font-weight: bold; font-size: 14px;")
        self.btn_stop.clicked.connect(self.stop_requested.emit)
        layout.addWidget(self.btn_stop)
        
        layout.addStretch()

    def get_rates(self): return float(self.sp_amp.value()), float(self.sp_lkn.value())
    
    def to_models(self) -> Tuple[Connections, SaveRoot, bool]:
        c = Connections(gate1=self.ed_g1.text(), gate2=self.ed_g2.text(), gate3=self.ed_g3.text(), daq_dev=self.ed_daq.text(), mono=self.ed_mono.text())
        s = SaveRoot(user=self.ed_user.text(), sample=self.ed_sample.text(), base=self.ed_base.text())
        return c, s, True

    # --- Persistence Logic ---
    def save_settings(self):
        s = QtCore.QSettings("MyLab", "TransportUI")
        s.setValue("addr/g1", self.ed_g1.text())
        s.setValue("addr/g2", self.ed_g2.text())
        s.setValue("addr/g3", self.ed_g3.text())
        s.setValue("addr/daq", self.ed_daq.text())
        s.setValue("addr/mono", self.ed_mono.text())
        s.setValue("path/user", self.ed_user.text())
        s.setValue("path/sample", self.ed_sample.text())
        s.setValue("path/base", self.ed_base.text())
        s.setValue("rates/amp", self.sp_amp.value())
        s.setValue("rates/lkn", self.sp_lkn.value())

    def load_settings(self):
        s = QtCore.QSettings("MyLab", "TransportUI")
        self.ed_g1.setText(str(s.value("addr/g1", self.conns.gate1)))
        self.ed_g2.setText(str(s.value("addr/g2", self.conns.gate2)))
        self.ed_g3.setText(str(s.value("addr/g3", self.conns.gate3)))
        self.ed_daq.setText(str(s.value("addr/daq", self.conns.daq_dev)))
        self.ed_mono.setText(str(s.value("addr/mono", self.conns.mono)))
        self.ed_user.setText(str(s.value("path/user", self.save_root.user)))
        self.ed_sample.setText(str(s.value("path/sample", self.save_root.sample)))
        self.ed_base.setText(str(s.value("path/base", self.save_root.base)))
        self.sp_amp.setValue(float(s.value("rates/amp", 1e7)))
        self.sp_lkn.setValue(float(s.value("rates/lkn", 100.0)))

# ----------------- Main Window -----------------
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Unified Dual‑Gate Measurement UI")
        self.resize(1400, 850)
        
        # --- Dock Setup ---
        self.conn_dock = ConnDock()
        self.conn_dock.load_settings() # Load saved GPIB addresses on startup
        self.conn_dock.stop_requested.connect(self.on_emergency_stop) # Link red button
        
        dock = QtWidgets.QDockWidget("Connections / Globals / Save"); dock.setWidget(self.conn_dock)
        dock.setFeatures(QtWidgets.QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)

        # References for tabs to share
        self.save_root = self.conn_dock.save_root
        self.connections = self.conn_dock.conns

        # --- Tabs Setup ---
        self.tabs = QtWidgets.QTabWidget(); self.setCentralWidget(self.tabs)
        
        # Tab 1: Dual Gate
        self.tab_dual = DualGateTab(self.save_root, self.connections, get_global_rates_callable=self.conn_dock.get_rates)
        self.tabs.addTab(self.tab_dual, "Dual‑gate Vds Sweep")
        
        # Tab 2: Co-Sweep
        # Note: We pass tab_dual's method to get AO items so we don't duplicate code
        self.tab_cosweep = CoSweepTab(self.save_root, self.connections, get_global_rates_callable=self.conn_dock.get_rates, get_ao_items_callable=self.tab_dual.get_ao_items_if_available)
        self.tabs.addTab(self.tab_cosweep, "Co‑sweep")
        
        # Tab 3: Photocurrent
        self.tab_photocurrent = PhotocurrentTab(self.save_root, self.connections, get_global_rates_callable=self.conn_dock.get_rates, get_ao_items_callable=self.tab_dual.get_ao_items_if_available)
        self.tabs.addTab(self.tab_photocurrent, "Photocurrent")

    # --- CRITICAL: Syncs text boxes from Dock to the Python objects used by workers ---
    def refresh_models_from_ui(self):
        c, s, _ = self.conn_dock.to_models()
        # Update shared objects in place
        self.save_root.user = s.user; self.save_root.sample = s.sample; self.save_root.base = s.base
        self.connections.gate1=c.gate1; self.connections.gate2=c.gate2; self.connections.gate3=c.gate3
        self.connections.daq_dev=c.daq_dev; self.connections.mono=c.mono

    def closeEvent(self, event):
        # Save settings (GPIB addresses, etc.) when you close the window
        self.conn_dock.save_settings()
        event.accept()

    def on_emergency_stop(self):
        # 1. Stop threads first
        tabs = [self.tab_dual, self.tab_cosweep, self.tab_photocurrent]
        for tab in tabs:
            if tab.worker: 
                tab.worker.request_stop()
                tab.log.appendPlainText("!!! EMERGENCY STOP REQUESTED !!!")

        # 2. Zero voltage for Keithley Gates (Always safe/desired to zero gates)
        for t in tabs:
            for gate_session in [t.s_g1, t.s_g2, t.s_g3]:
                if gate_session:
                    try: gate_session.ramp_voltage(0.0, 1.0)
                    except: pass

        # 3. Smart Zero for DAQ
        # Only zero the DAQ AO if it is explicitly selected as the Vds source in the active tab.
        daq_zeroed_log = []
        for t in tabs:
            if t.s_daq:
                # Check config
                vds_source_text = t.cbo_source.currentText()
                if "NI DAQ" in vds_source_text:
                    try:
                        chan_idx = int(vds_source_text.split()[-1].replace('ao',''))
                        t.s_daq.ramp_voltage(chan_idx, 0.0, 0.1)
                        daq_zeroed_log.append(f"Zeroed ao{chan_idx} (Vds)")
                    except Exception as e:
                        print(f"Failed to zero DAQ: {e}")
        
        msg = "Stop signal sent to all workers.\n\n"
        msg += "Actions taken:\n"
        msg += "- All Keithley Gates (G1, G2, G3) ramped to 0V.\n"
        if daq_zeroed_log:
            msg += f"- DAQ Vds Source: {', '.join(set(daq_zeroed_log))}\n"
        else:
            msg += "- DAQ AO channels were NOT touched (not set as Vds source).\n"

        QtWidgets.QMessageBox.critical(self, "Emergency Stop", msg)

def launch_in_notebook(show: bool = True) -> MainWindow:
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(sys.argv)
    w = MainWindow()
    if show: w.show()
    return w

if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow(); w.show()
    sys.exit(app.exec())
