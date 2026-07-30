"""
gui.py — Interface principale : Robot Log Viewer

- Bouton "Ouvrir un dossier de logs" : détecte automatiquement les fichiers
  ..._MODEL.log / ..._pos.log / ..._boot.log / dmesg.txt dans le dossier
  choisi (comme dans l'archive .tar exportée par le robot).
- Carte façon appli officielle : zone tondue en vert plein sur fond clair,
  station de charge, position du robot. Molette = zoom, glisser = déplacer,
  double-clic = vue entière, clic = choisir un moment.
- Mode "Show time path" : à partir du moment choisi, Espace fait avancer
  le robot seconde par seconde sur la carte (Maj+Espace pour reculer),
  le chemin parcouru se dessine en bleu et les logs suivent.
"""

from __future__ import annotations

import os
import sys
import bisect
import statistics
from datetime import datetime, timedelta

from PySide6.QtCore import Qt, QEvent
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFileDialog, QLineEdit, QComboBox, QListWidget,
    QListWidgetItem, QSplitter, QGroupBox, QFormLayout, QDateTimeEdit,
    QCheckBox, QStatusBar, QMessageBox, QTabWidget, QTextEdit,
    QAbstractItemView
)
from PySide6.QtGui import QColor

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from matplotlib.lines import Line2D

from parser import load_session_from_folder, load_session, RobotSession, TrackPoint


APP_TITLE = "Robot Log Viewer"

# Palette "appli officielle" : fond clair, zone tondue verte
COL_BG = "#f4f4f1"
COL_ZONE = "#28a95c"       # vert plein de la zone tondue
COL_ZONE_PALE = "#c4e9d2"  # même zone, estompée (mode analyse)
COL_PATH = "#1565c0"       # chemin parcouru en mode analyse
COL_ROBOT = "#111111"      # position du robot
COL_STATION_EDGE = "#111111"
COL_SELECT = "#d81b60"     # anneau de sélection


class TrackCanvas(FigureCanvas):
    """Carte interactive :
    - molette : zoom centré sur le curseur
    - clic gauche + glisser : déplacement (pan)
    - double-clic : réinitialise la vue
    - clic simple : sélectionne le moment correspondant
      (le callback `on_point_clicked` est appelé avec le TrackPoint)
    """

    def __init__(self):
        self.fig = Figure(figsize=(5, 5))
        super().__init__(self.fig)
        self.ax = self.fig.add_subplot(111)
        self.fig.subplots_adjust(left=0.02, right=0.98, top=0.98, bottom=0.02)

        self._pts: list[TrackPoint] = []
        self._ts: list[datetime] = []
        self._sel_marker = None
        self._trail = None
        self._robot_marker = None
        self._analysis_start: datetime | None = None
        self.on_point_clicked = None  # callback(TrackPoint), posé par MainWindow

        self._press = None      # état du glisser : (x_px, y_px, xlim, ylim, xdata, ydata)
        self._dragging = False

        self.mpl_connect("scroll_event", self._on_scroll)
        self.mpl_connect("button_press_event", self._on_press)
        self.mpl_connect("motion_notify_event", self._on_motion)
        self.mpl_connect("button_release_event", self._on_release)

        self._reset_axes()

    def _reset_axes(self):
        self.ax.clear()
        self.ax.set_facecolor(COL_BG)
        self.fig.patch.set_facecolor(COL_BG)
        self.ax.set_xticks([])
        self.ax.set_yticks([])
        for spine in self.ax.spines.values():
            spine.set_color("#dddddd")
        self.ax.set_aspect("equal", adjustable="datalim")
        self._sel_marker = None
        self._trail = None
        self._robot_marker = None

    # ------------------------------------------------------------------
    # Interactions souris
    # ------------------------------------------------------------------
    def _on_scroll(self, event):
        if event.inaxes != self.ax or event.xdata is None:
            return
        scale = 1 / 1.25 if event.button == "up" else 1.25
        x, y = event.xdata, event.ydata
        xlim = self.ax.get_xlim()
        ylim = self.ax.get_ylim()
        self.ax.set_xlim([x + (v - x) * scale for v in xlim])
        self.ax.set_ylim([y + (v - y) * scale for v in ylim])
        self.draw_idle()

    def _on_press(self, event):
        if event.inaxes != self.ax or event.button != 1:
            return
        if event.dblclick:
            self._press = None
            self.reset_view()
            return
        self._press = (event.x, event.y, self.ax.get_xlim(), self.ax.get_ylim(),
                       event.xdata, event.ydata)
        self._dragging = False

    def _on_motion(self, event):
        if self._press is None or event.x is None:
            return
        x0, y0, xlim, ylim, _, _ = self._press
        dx_px = event.x - x0
        dy_px = event.y - y0
        if not self._dragging and abs(dx_px) < 5 and abs(dy_px) < 5:
            return
        self._dragging = True
        dx = dx_px * (xlim[1] - xlim[0]) / self.ax.bbox.width
        dy = dy_px * (ylim[1] - ylim[0]) / self.ax.bbox.height
        self.ax.set_xlim(xlim[0] - dx, xlim[1] - dx)
        self.ax.set_ylim(ylim[0] - dy, ylim[1] - dy)
        self.draw_idle()

    def _on_release(self, event):
        press, dragging = self._press, self._dragging
        self._press = None
        self._dragging = False
        if press is None or dragging or event.button != 1:
            return
        _, _, _, _, xdata, ydata = press
        if xdata is None or not self._pts or self.on_point_clicked is None:
            return
        nearest = min(self._pts, key=lambda p: (p.x - xdata) ** 2 + (p.y - ydata) ** 2)
        self.on_point_clicked(nearest)

    def reset_view(self):
        if not self._pts:
            return
        xs = [p.x for p in self._pts] + [0.0]
        ys = [p.y for p in self._pts] + [0.0]
        margin_x = max((max(xs) - min(xs)) * 0.05, 1.0)
        margin_y = max((max(ys) - min(ys)) * 0.05, 1.0)
        self.ax.set_xlim(min(xs) - margin_x, max(xs) + margin_x)
        self.ax.set_ylim(min(ys) - margin_y, max(ys) + margin_y)
        self.draw_idle()

    # ------------------------------------------------------------------
    # Aides de tracé
    # ------------------------------------------------------------------
    def _filter_points(self, points, filter_outliers):
        pts = points
        if filter_outliers and len(pts) > 10:
            mx = statistics.median(p.x for p in pts)
            my = statistics.median(p.y for p in pts)
            pts = [p for p in pts if abs(p.x - mx) < 300 and abs(p.y - my) < 300]
        return pts

    def _draw_zone(self, pale: bool):
        xs = [p.x for p in self._pts]
        ys = [p.y for p in self._pts]
        color = COL_ZONE_PALE if pale else COL_ZONE
        self.ax.scatter(xs, ys, color=color, s=14, linewidths=0, zorder=1)

    def _draw_station(self):
        self.ax.plot(0, 0, marker="o", markersize=13, markerfacecolor="white",
                     markeredgecolor=COL_STATION_EDGE, markeredgewidth=2,
                     linestyle="", zorder=5)
        self.ax.annotate("⚡", (0, 0), ha="center", va="center",
                         fontsize=8, zorder=6)

    def _legend(self, analysis: bool):
        handles = [
            Line2D([], [], marker="o", linestyle="", color=COL_ZONE,
                   label="Zone tondue"),
            Line2D([], [], marker="o", linestyle="", markerfacecolor="white",
                   markeredgecolor=COL_STATION_EDGE, color="white",
                   label="Station de charge"),
            Line2D([], [], marker="o", linestyle="", color=COL_ROBOT,
                   label="Robot"),
        ]
        if analysis:
            handles.append(Line2D([], [], color=COL_PATH, linewidth=2,
                                  label="Chemin parcouru"))
        else:
            handles.append(Line2D([], [], marker="o", linestyle="",
                                  markerfacecolor="none",
                                  markeredgecolor=COL_SELECT, color=COL_SELECT,
                                  label="Moment sélectionné"))
        self.ax.legend(handles=handles, facecolor="white", edgecolor="#cccccc",
                       loc="upper right", fontsize=8, framealpha=0.9)

    # ------------------------------------------------------------------
    # Vue normale : zone tondue + station + dernière position du robot
    # ------------------------------------------------------------------
    def plot_track(self, points: list[TrackPoint], filter_outliers: bool = True):
        self._reset_axes()
        self._analysis_start = None
        self._pts = self._filter_points(points, filter_outliers)
        self._ts = [p.ts for p in self._pts]
        if not self._pts:
            self.draw()
            return

        self._draw_zone(pale=False)
        self._draw_station()
        last = self._pts[-1]
        self.ax.plot(last.x, last.y, marker="o", markersize=10,
                     markerfacecolor=COL_ROBOT, markeredgecolor="white",
                     markeredgewidth=1.5, linestyle="", zorder=6)
        (self._sel_marker,) = self.ax.plot(
            [], [], marker="o", markersize=14, markerfacecolor="none",
            markeredgecolor=COL_SELECT, markeredgewidth=2.5, linestyle="",
            zorder=7
        )
        self._legend(analysis=False)
        self.reset_view()
        self.draw()

    def select_time(self, ts: datetime) -> TrackPoint | None:
        """Anneau rose sur le point le plus proche de `ts` (vue normale)."""
        if not self._pts or self._sel_marker is None or ts is None:
            return None
        best = min(self._pts, key=lambda p: abs((p.ts - ts).total_seconds()))
        self._sel_marker.set_data([best.x], [best.y])
        self.draw_idle()
        return best

    # ------------------------------------------------------------------
    # Mode "Show time path"
    # ------------------------------------------------------------------
    def enter_analysis(self, start: datetime):
        """Passe la carte en mode analyse : zone estompée, le chemin du robot
        se dessinera en bleu à partir de `start` (le zoom courant est gardé)."""
        if not self._pts:
            return
        xlim, ylim = self.ax.get_xlim(), self.ax.get_ylim()
        self._reset_axes()
        self._analysis_start = start
        self._draw_zone(pale=True)
        self._draw_station()
        (self._trail,) = self.ax.plot([], [], color=COL_PATH, linewidth=2.5,
                                      solid_capstyle="round", zorder=5)
        (self._robot_marker,) = self.ax.plot(
            [], [], marker="o", markersize=11, markerfacecolor=COL_ROBOT,
            markeredgecolor="white", markeredgewidth=1.5, linestyle="", zorder=6
        )
        self._legend(analysis=True)
        self.ax.set_xlim(xlim)
        self.ax.set_ylim(ylim)
        self.draw()

    def show_until(self, ts: datetime) -> TrackPoint | None:
        """Affiche la situation à l'instant `ts` : chemin depuis le départ de
        l'analyse + robot à sa position de ce moment-là."""
        if self._analysis_start is None or not self._pts:
            return None
        i0 = bisect.bisect_left(self._ts, self._analysis_start)
        i1 = bisect.bisect_right(self._ts, ts)
        seg = self._pts[i0:i1]
        if seg:
            self._trail.set_data([p.x for p in seg], [p.y for p in seg])
            current = seg[-1]
        else:
            # pas encore de position depuis le départ : robot au point le plus proche
            self._trail.set_data([], [])
            current = min(self._pts, key=lambda p: abs((p.ts - ts).total_seconds()))
        self._robot_marker.set_data([current.x], [current.y])
        self.draw_idle()
        return current


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1300, 800)

        self.session: RobotSession | None = None
        self.analysis_start: datetime | None = None  # moment choisi (clic carte / log)
        self.analysis_time: datetime | None = None   # curseur du mode Show time path

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # --- Barre du haut : ouverture des fichiers ---
        top_bar = QHBoxLayout()
        self.btn_open_folder = QPushButton("Ouvrir un dossier de logs…")
        self.btn_open_folder.clicked.connect(self.on_open_folder)
        self.btn_open_files = QPushButton("Ouvrir des fichiers…")
        self.btn_open_files.clicked.connect(self.on_open_files)
        self.lbl_loaded = QLabel("Aucun fichier chargé.")
        top_bar.addWidget(self.btn_open_folder)
        top_bar.addWidget(self.btn_open_files)
        top_bar.addWidget(self.lbl_loaded, stretch=1)
        root.addLayout(top_bar)

        # --- Filtre temporel ---
        filter_bar = QHBoxLayout()
        filter_bar.addWidget(QLabel("Début :"))
        self.dt_start = QDateTimeEdit()
        self.dt_start.setCalendarPopup(True)
        filter_bar.addWidget(self.dt_start)
        filter_bar.addWidget(QLabel("Fin :"))
        self.dt_end = QDateTimeEdit()
        self.dt_end.setCalendarPopup(True)
        filter_bar.addWidget(self.dt_end)
        self.chk_outliers = QCheckBox("Filtrer les points aberrants")
        self.chk_outliers.setChecked(True)
        filter_bar.addWidget(self.chk_outliers)
        self.btn_apply = QPushButton("Appliquer le filtre")
        self.btn_apply.clicked.connect(self.refresh_view)
        filter_bar.addWidget(self.btn_apply)
        filter_bar.addStretch(1)
        root.addLayout(filter_bar)

        # --- Zone principale : splitter (carte | infos+logs) ---
        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, stretch=1)

        # Gauche : carte + barre d'analyse
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.canvas = TrackCanvas()
        self.canvas.on_point_clicked = self.on_map_point_clicked
        left_layout.addWidget(self.canvas, stretch=1)

        analysis_bar = QHBoxLayout()
        self.btn_timepath = QPushButton("▶  Show time path")
        self.btn_timepath.setCheckable(True)
        self.btn_timepath.toggled.connect(self.on_timepath_toggled)
        analysis_bar.addWidget(self.btn_timepath)
        self.lbl_time = QLabel("")
        self.lbl_time.setStyleSheet(
            "font-size: 17px; font-weight: bold; color: #1565c0; padding-left: 10px;"
        )
        analysis_bar.addWidget(self.lbl_time, stretch=1)
        left_layout.addLayout(analysis_bar)

        hint = QLabel(
            "1. Clic sur la carte : choisir un moment  •  2. « Show time path » puis "
            "maintenir Espace : le robot avance seconde par seconde (Maj+Espace : reculer)\n"
            "Molette : zoom  •  Glisser : déplacer  •  Double-clic : vue entière"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888888; font-size: 10px; padding: 2px;")
        left_layout.addWidget(hint)
        splitter.addWidget(left)

        # Droite : onglets Infos / Logs
        right = QWidget()
        right_layout = QVBoxLayout(right)
        self.tabs = QTabWidget()
        right_layout.addWidget(self.tabs)

        # Onglet infos
        info_box = QGroupBox("Informations robot")
        form = QFormLayout(info_box)
        self.lbl_model = QLabel("-")
        self.lbl_serial = QLabel("-")
        self.lbl_firmware = QLabel("-")
        self.lbl_export_date = QLabel("-")
        self.lbl_battery = QLabel("-")
        self.lbl_blade = QLabel("-")
        self.lbl_worktime = QLabel("-")
        self.lbl_distance = QLabel("-")
        self.lbl_boundary = QLabel("-")
        for label, widget in [
            ("Modèle", self.lbl_model), ("Numéro de série", self.lbl_serial),
            ("Firmware", self.lbl_firmware), ("Date d'export", self.lbl_export_date),
            ("Recharges batterie", self.lbl_battery), ("Temps de lame", self.lbl_blade),
            ("Temps de travail total", self.lbl_worktime),
            ("Distance totale", self.lbl_distance),
            ("Longueur de la bordure", self.lbl_boundary),
        ]:
            form.addRow(label + " :", widget)

        self.txt_schedule = QTextEdit()
        self.txt_schedule.setReadOnly(True)
        self.txt_schedule.setMaximumHeight(140)
        form.addRow("Planning :", self.txt_schedule)
        self.tabs.addTab(info_box, "Infos")

        # Onglet logs
        self.logs_widget = QWidget()
        logs_layout = QVBoxLayout(self.logs_widget)
        search_bar = QHBoxLayout()
        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText("Rechercher dans les logs…")
        self.txt_search.returnPressed.connect(self.refresh_log_list)
        self.combo_level = QComboBox()
        self.combo_level.addItems(["Tous niveaux", "INFO", "WARN", "ERROR", "-"])
        self.combo_level.currentIndexChanged.connect(self.refresh_log_list)
        self.combo_source = QComboBox()
        self.combo_source.addItem("Tous fichiers")
        self.combo_source.currentIndexChanged.connect(self.refresh_log_list)
        btn_search = QPushButton("Filtrer")
        btn_search.clicked.connect(self.refresh_log_list)
        search_bar.addWidget(self.txt_search, stretch=1)
        search_bar.addWidget(self.combo_level)
        search_bar.addWidget(self.combo_source)
        search_bar.addWidget(btn_search)
        logs_layout.addLayout(search_bar)

        self.list_logs = QListWidget()
        self.list_logs.setStyleSheet("font-family: monospace; font-size: 11px;")
        self.list_logs.currentRowChanged.connect(self.on_log_row_changed)
        logs_layout.addWidget(self.list_logs, stretch=1)

        self.tabs.addTab(self.logs_widget, "Historique des logs")

        splitter.addWidget(right)
        splitter.setSizes([650, 650])

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Prêt.")

        # Capte la touche Espace partout dans la fenêtre (sauf champs de saisie)
        QApplication.instance().installEventFilter(self)

    # ------------------------------------------------------------------
    # Espace : avance d'une seconde en mode Show time path (Maj = recule),
    # maintenir la touche fait défiler en continu (auto-répétition clavier).
    # Hors mode analyse, Espace fait défiler la liste des logs.
    # ------------------------------------------------------------------
    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress and event.key() == Qt.Key_Space:
            fw = QApplication.focusWidget()
            if isinstance(fw, (QLineEdit, QTextEdit, QDateTimeEdit)):
                return super().eventFilter(obj, event)
            step = -1 if event.modifiers() & Qt.ShiftModifier else 1
            if self.btn_timepath.isChecked():
                self.step_analysis(step)
                return True
            if fw is self.list_logs:
                row = self.list_logs.currentRow() + step
                row = max(0, min(row, self.list_logs.count() - 1))
                self.list_logs.setCurrentRow(row)
                return True
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------
    # Mode "Show time path"
    # ------------------------------------------------------------------
    def on_timepath_toggled(self, checked: bool):
        if checked:
            if not self.session or not self.canvas._pts:
                self.btn_timepath.setChecked(False)
                self.statusBar().showMessage("Chargez d'abord des logs.")
                return
            start = self.analysis_start or self.canvas._pts[0].ts
            self.analysis_start = start
            self.analysis_time = start
            self.canvas.enter_analysis(start)
            self.canvas.show_until(start)
            self.btn_timepath.setText("⏹  Show time path (actif)")
            self.lbl_time.setText(f"⏱  {start:%d/%m/%Y %H:%M:%S}")
            self.sync_logs_to_time(start)
            self.statusBar().showMessage(
                "Mode analyse : maintenez Espace pour avancer seconde par seconde, "
                "Maj+Espace pour reculer, clic sur la carte pour repartir d'ailleurs."
            )
            self.setFocus()
        else:
            self.btn_timepath.setText("▶  Show time path")
            self.lbl_time.setText("")
            self.analysis_time = None
            self.refresh_track()

    def step_analysis(self, seconds: int):
        if self.analysis_time is None or not self.canvas._pts:
            return
        t = self.analysis_time + timedelta(seconds=seconds)
        t_min = self.analysis_start
        t_max = self.canvas._pts[-1].ts
        t = max(t_min, min(t, t_max))
        self.analysis_time = t
        self.canvas.show_until(t)
        self.lbl_time.setText(f"⏱  {t:%d/%m/%Y %H:%M:%S}")
        self.sync_logs_to_time(t)

    def sync_logs_to_time(self, ts: datetime):
        """Cale la liste des logs sur la ligne la plus proche de `ts`."""
        best_row, best_gap = -1, None
        for i in range(self.list_logs.count()):
            lts = self.list_logs.item(i).data(Qt.UserRole)
            if lts is None:
                continue
            gap = abs((lts - ts).total_seconds())
            if best_gap is None or gap < best_gap:
                best_row, best_gap = i, gap
        if best_row < 0:
            return
        self.list_logs.blockSignals(True)
        self.list_logs.setCurrentRow(best_row)
        self.list_logs.blockSignals(False)
        self.list_logs.scrollToItem(
            self.list_logs.item(best_row),
            QAbstractItemView.ScrollHint.PositionAtCenter,
        )

    # ------------------------------------------------------------------
    # Synchronisation carte <-> logs
    # ------------------------------------------------------------------
    def on_map_point_clicked(self, point: TrackPoint):
        """Clic sur la carte : choisit ce moment comme point de départ."""
        self.analysis_start = point.ts
        if self.btn_timepath.isChecked():
            # redémarre l'analyse à partir de ce moment
            self.analysis_time = point.ts
            self.canvas.enter_analysis(point.ts)
            self.canvas.show_until(point.ts)
            self.lbl_time.setText(f"⏱  {point.ts:%d/%m/%Y %H:%M:%S}")
            self.sync_logs_to_time(point.ts)
            return
        self.canvas.select_time(point.ts)
        self.sync_logs_to_time(point.ts)
        self.tabs.setCurrentWidget(self.logs_widget)
        self.statusBar().showMessage(
            f"Moment choisi : {point.ts:%d/%m/%Y %H:%M:%S} — cliquez sur "
            "« Show time path » pour rejouer le comportement du robot."
        )

    def on_log_row_changed(self, row: int):
        """Sélection d'une ligne de log : place l'anneau sur la carte."""
        if row < 0 or not self.session or self.btn_timepath.isChecked():
            return
        item = self.list_logs.item(row)
        ts = item.data(Qt.UserRole) if item else None
        if ts is None:
            return
        self.analysis_start = ts
        self.canvas.select_time(ts)

    # ------------------------------------------------------------------
    # Chargement
    # ------------------------------------------------------------------
    def on_open_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Choisir le dossier de logs du robot")
        if not folder:
            return
        try:
            self.session = load_session_from_folder(folder)
        except Exception as e:
            QMessageBox.critical(self, "Erreur", f"Impossible de lire ce dossier :\n{e}")
            return
        self._after_load(folder)

    def on_open_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Choisir un ou plusieurs fichiers de logs", "",
            "Fichiers logs (*.log *.txt);;Tous les fichiers (*)"
        )
        if not files:
            return

        main = pos = path = boot = dmesg = None
        for f in files:
            base = os.path.basename(f).lower()
            if base.startswith("dmesg"):
                dmesg = f
            elif base.endswith("_pos.log"):
                pos = f
            elif base.endswith("_path.log"):
                path = f
            elif base.endswith("_boot.log"):
                boot = f
            else:
                main = f

        try:
            self.session = load_session(main=main, pos=pos, path=path, boot=boot, dmesg=dmesg)
        except Exception as e:
            QMessageBox.critical(self, "Erreur", f"Impossible de lire ces fichiers :\n{e}")
            return
        self._after_load(", ".join(os.path.basename(f) for f in files))

    def _after_load(self, source_label: str):
        s = self.session
        if not s or not s.files_loaded:
            QMessageBox.warning(self, "Aucune donnée", "Aucun fichier de log reconnu n'a été trouvé.")
            return

        self.lbl_loaded.setText(f"Chargé : {source_label}  —  fichiers : {', '.join(s.files_loaded)}")

        self.lbl_model.setText(s.model or "-")
        self.lbl_serial.setText(s.serial or "-")
        self.lbl_firmware.setText(s.firmware or "-")
        self.lbl_export_date.setText(s.export_date or "-")
        self.lbl_battery.setText(s.battery_recharged or "-")
        self.lbl_blade.setText(s.blade_running_time or "-")
        self.lbl_worktime.setText(s.total_work_time or "-")
        self.lbl_distance.setText(s.total_distance or "-")
        self.lbl_boundary.setText(s.boundary_length or "-")
        self.txt_schedule.setPlainText("\n".join(s.schedule))

        # Filtre de dates : bornes = première/dernière date valide trouvée
        valid_ts = [p.ts for p in s.track] + [l.ts for l in s.lines if l.ts]
        if valid_ts:
            self.dt_start.setDateTime(min(valid_ts))
            self.dt_end.setDateTime(max(valid_ts))

        # Liste des fichiers sources pour le filtre de logs
        self.combo_source.blockSignals(True)
        self.combo_source.clear()
        self.combo_source.addItem("Tous fichiers")
        self.combo_source.addItems(sorted(set(s.files_loaded)))
        self.combo_source.blockSignals(False)

        # repart d'un état propre
        self.analysis_start = None
        if self.btn_timepath.isChecked():
            self.btn_timepath.setChecked(False)

        self.statusBar().showMessage(
            f"{len(s.track)} points de position — {len(s.lines)} lignes de log."
        )

        self.refresh_view()

    # ------------------------------------------------------------------
    # Rafraîchissement
    # ------------------------------------------------------------------
    def refresh_view(self):
        self.refresh_track()
        self.refresh_log_list()

    def refresh_track(self):
        if not self.session:
            return
        start = self.dt_start.dateTime().toPython()
        end = self.dt_end.dateTime().toPython()
        pts = [p for p in self.session.track if start <= p.ts <= end]
        self.canvas.plot_track(pts, filter_outliers=self.chk_outliers.isChecked())

    def refresh_log_list(self):
        if not self.session:
            return
        start = self.dt_start.dateTime().toPython()
        end = self.dt_end.dateTime().toPython()
        needle = self.txt_search.text().strip().lower()
        level = self.combo_level.currentText()
        source = self.combo_source.currentText()

        self.list_logs.blockSignals(True)
        self.list_logs.clear()
        count = 0
        MAX_DISPLAY = 5000  # évite de figer l'UI sur des logs de plusieurs centaines de milliers de lignes

        for l in self.session.lines:
            if l.ts is not None and not (start <= l.ts <= end):
                continue
            if level != "Tous niveaux" and l.level != level:
                continue
            if source != "Tous fichiers" and l.source != source:
                continue
            if needle and needle not in l.raw.lower():
                continue

            item = QListWidgetItem(l.raw)
            item.setData(Qt.UserRole, l.ts)
            if l.level == "ERROR":
                item.setForeground(QColor("#ff6666"))
            elif l.level == "WARN":
                item.setForeground(QColor("#cc8800"))
            self.list_logs.addItem(item)
            count += 1
            if count >= MAX_DISPLAY:
                self.list_logs.addItem(QListWidgetItem(
                    f"… affichage limité à {MAX_DISPLAY} lignes, affinez le filtre pour voir le reste."
                ))
                break

        self.list_logs.blockSignals(False)
        self.statusBar().showMessage(f"{count} lignes affichées sur {len(self.session.lines)}.")


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
