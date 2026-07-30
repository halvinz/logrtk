"""
gui.py — Interface principale : Robot Log Viewer

- Bouton "Ouvrir un dossier de logs" : détecte automatiquement les fichiers
  ..._MODEL.log / ..._pos.log / ..._boot.log / dmesg.txt dans le dossier
  choisi (comme dans l'archive .tar exportée par le robot).
- Bouton "Ouvrir des fichiers..." : sélection manuelle fichier par fichier
  si l'auto-détection ne convient pas.
- Tracé du trajet du robot (à partir des positions SLAM) avec dégradé de
  couleur dans le temps, filtrage par plage de dates.
- Carte interactive : molette = zoom, glisser = déplacer, double-clic =
  réinitialiser la vue, clic = caler les logs sur cette position.
- Visionneuse de logs avec recherche texte + filtre par niveau + fichier.
  Espace (maintenu) fait défiler les logs, le marqueur suit sur la carte.
"""

from __future__ import annotations

import os
import sys
import statistics
from datetime import datetime

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


class TrackCanvas(FigureCanvas):
    """Carte du trajet, interactive :
    - molette : zoom centré sur le curseur
    - clic gauche + glisser : déplacement (pan)
    - double-clic : réinitialise la vue
    - clic simple : sélectionne le point de trajet le plus proche
      (le callback `on_point_clicked` est alors appelé avec ce point)
    """

    def __init__(self):
        self.fig = Figure(figsize=(5, 5))
        super().__init__(self.fig)
        self.ax = self.fig.add_subplot(111)
        self._pts: list[TrackPoint] = []
        self._sel_marker = None
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
        self.ax.set_facecolor("#101010")
        self.fig.patch.set_facecolor("#101010")
        self.ax.tick_params(colors="#cccccc")
        for spine in self.ax.spines.values():
            spine.set_color("#444444")
        self.ax.set_aspect("equal", adjustable="datalim")
        self._sel_marker = None

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
    # Tracé
    # ------------------------------------------------------------------
    def plot_track(self, points: list[TrackPoint], filter_outliers: bool = True):
        self._reset_axes()
        self._pts = []
        if not points:
            self.draw()
            return

        pts = points
        if filter_outliers and len(pts) > 10:
            mx = statistics.median(p.x for p in pts)
            my = statistics.median(p.y for p in pts)
            pts = [p for p in pts if abs(p.x - mx) < 300 and abs(p.y - my) < 300]

        if not pts:
            self.draw()
            return

        self._pts = pts
        xs = [p.x for p in pts]
        ys = [p.y for p in pts]

        # Dégradé de couleur du début (bleu) à la fin (rouge), comme l'outil d'origine
        n = len(xs)
        self.ax.scatter(xs, ys, c=range(n), cmap="coolwarm", s=3, linewidths=0)
        self.ax.plot(xs[0], ys[0], "o", color="lime", markersize=8)
        self.ax.plot(xs[-1], ys[-1], "o", color="yellow", markersize=8)
        # Station de charge : origine de la carte SLAM (le robot démarre à sa base)
        self.ax.plot(0, 0, marker="s", color="orange", markersize=10, linestyle="")
        # Marqueur de la position sélectionnée (synchronisé avec les logs)
        (self._sel_marker,) = self.ax.plot(
            [], [], marker="o", markersize=13, markerfacecolor="none",
            markeredgecolor="cyan", markeredgewidth=2, linestyle=""
        )

        handles = [
            Line2D([], [], marker="s", linestyle="", color="orange",
                   label="Station de charge (origine)"),
            Line2D([], [], marker="o", linestyle="", color="lime",
                   label="Départ du trajet"),
            Line2D([], [], marker="o", linestyle="", color="yellow",
                   label="Dernière position (robot)"),
            Line2D([], [], marker="o", linestyle="", color="#4a6fe3",
                   label="Tonte — début (bleu)"),
            Line2D([], [], marker="o", linestyle="", color="#d63b3b",
                   label="Tonte — fin (rouge)"),
            Line2D([], [], marker="o", linestyle="", markerfacecolor="none",
                   markeredgecolor="cyan", color="cyan",
                   label="Position sélectionnée"),
        ]
        self.ax.legend(handles=handles, facecolor="#222222", labelcolor="white",
                       edgecolor="#444444", loc="upper right", fontsize=8)
        self.reset_view()
        self.draw()

    def select_time(self, ts: datetime) -> TrackPoint | None:
        """Place le marqueur cyan sur le point de trajet le plus proche de `ts`
        (sans toucher au zoom en cours). Retourne le point trouvé."""
        if not self._pts or self._sel_marker is None or ts is None:
            return None
        best = min(self._pts, key=lambda p: abs((p.ts - ts).total_seconds()))
        self._sel_marker.set_data([best.x], [best.y])
        self.draw_idle()
        return best


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1300, 800)

        self.session: RobotSession | None = None

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

        # --- Zone principale : splitter (trajet | infos+logs) ---
        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, stretch=1)

        # Gauche : trajet + aide
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.canvas = TrackCanvas()
        self.canvas.on_point_clicked = self.on_map_point_clicked
        left_layout.addWidget(self.canvas, stretch=1)
        hint = QLabel(
            "Molette : zoom  •  Glisser : déplacer  •  Double-clic : vue entière  •  "
            "Clic : caler les logs ici  •  Espace : défiler les logs (Maj+Espace : reculer)"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #999999; font-size: 10px; padding: 2px;")
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
        self.list_logs.installEventFilter(self)
        logs_layout.addWidget(self.list_logs, stretch=1)

        self.tabs.addTab(self.logs_widget, "Historique des logs")

        splitter.addWidget(right)
        splitter.setSizes([650, 650])

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Prêt.")

    # ------------------------------------------------------------------
    # Espace = défiler les logs (Maj+Espace = reculer) ; maintenir la
    # touche enfoncée fait défiler en continu (auto-répétition clavier).
    # ------------------------------------------------------------------
    def eventFilter(self, obj, event):
        if obj is self.list_logs and event.type() == QEvent.KeyPress \
                and event.key() == Qt.Key_Space:
            step = -1 if event.modifiers() & Qt.ShiftModifier else 1
            row = self.list_logs.currentRow() + step
            row = max(0, min(row, self.list_logs.count() - 1))
            self.list_logs.setCurrentRow(row)
            return True
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------
    # Synchronisation carte <-> logs
    # ------------------------------------------------------------------
    def on_map_point_clicked(self, point: TrackPoint):
        """Clic sur la carte : cale la liste des logs sur le moment où le
        robot était à cette position."""
        best_row, best_gap = -1, None
        for i in range(self.list_logs.count()):
            ts = self.list_logs.item(i).data(Qt.UserRole)
            if ts is None:
                continue
            gap = abs((ts - point.ts).total_seconds())
            if best_gap is None or gap < best_gap:
                best_row, best_gap = i, gap
        if best_row < 0:
            self.statusBar().showMessage(
                "Aucune ligne de log affichée pour cette période — élargissez les filtres."
            )
            return
        self.tabs.setCurrentWidget(self.logs_widget)
        self.list_logs.setCurrentRow(best_row)  # déclenche aussi le marqueur carte
        self.list_logs.scrollToItem(
            self.list_logs.item(best_row),
            QAbstractItemView.ScrollHint.PositionAtCenter,
        )
        self.list_logs.setFocus()
        self.statusBar().showMessage(
            f"Position du {point.ts:%d/%m/%Y %H:%M:%S} — logs calés dessus. "
            "Espace pour avancer, Maj+Espace pour reculer."
        )

    def on_log_row_changed(self, row: int):
        """Sélection d'une ligne de log : déplace le marqueur sur la carte."""
        if row < 0 or not self.session:
            return
        item = self.list_logs.item(row)
        ts = item.data(Qt.UserRole) if item else None
        if ts is None:
            return
        point = self.canvas.select_time(ts)
        if point is not None:
            gap = abs((point.ts - ts).total_seconds())
            note = f" (position la plus proche : ±{gap:.0f} s)" if gap > 2 else ""
            self.statusBar().showMessage(f"Log du {ts:%d/%m/%Y %H:%M:%S}{note}")

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
            if l.ts is None and (start != self.dt_start.minimumDateTime().toPython()):
                # lignes sans timestamp : on les garde uniquement si aucun filtre actif n'exclut tout
                pass
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
                item.setForeground(QColor("#ffcc66"))
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
