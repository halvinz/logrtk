"""
gui.py — Interface principale : Robot Log Viewer

- Bouton "Ouvrir un dossier de logs" : détecte automatiquement les fichiers
  ..._MODEL.log / ..._pos.log / ..._boot.log / dmesg.txt dans le dossier
  choisi (comme dans l'archive .tar exportée par le robot).
- Bouton "Ouvrir des fichiers..." : sélection manuelle fichier par fichier
  si l'auto-détection ne convient pas.
- Tracé du trajet du robot (à partir des positions SLAM) avec dégradé de
  couleur dans le temps, filtrage par plage de dates.
- Visionneuse de logs avec recherche texte + filtre par niveau + fichier.
"""

from __future__ import annotations

import os
import sys
import statistics
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFileDialog, QLineEdit, QComboBox, QListWidget,
    QListWidgetItem, QSplitter, QGroupBox, QFormLayout, QDateTimeEdit,
    QCheckBox, QStatusBar, QMessageBox, QTabWidget, QTextEdit
)
from PySide6.QtGui import QColor

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

from parser import load_session_from_folder, load_session, RobotSession, TrackPoint


APP_TITLE = "Robot Log Viewer"


class TrackCanvas(FigureCanvas):
    def __init__(self):
        self.fig = Figure(figsize=(5, 5))
        super().__init__(self.fig)
        self.ax = self.fig.add_subplot(111)
        self._reset_axes()

    def _reset_axes(self):
        self.ax.clear()
        self.ax.set_facecolor("#101010")
        self.fig.patch.set_facecolor("#101010")
        self.ax.tick_params(colors="#cccccc")
        for spine in self.ax.spines.values():
            spine.set_color("#444444")
        self.ax.set_aspect("equal", adjustable="datalim")

    def plot_track(self, points: list[TrackPoint], filter_outliers: bool = True):
        self._reset_axes()
        if not points:
            self.draw()
            return

        xs = [p.x for p in points]
        ys = [p.y for p in points]

        if filter_outliers and len(xs) > 10:
            mx, my = statistics.median(xs), statistics.median(ys)
            keep = [
                i for i in range(len(xs))
                if abs(xs[i] - mx) < 300 and abs(ys[i] - my) < 300
            ]
            xs = [xs[i] for i in keep]
            ys = [ys[i] for i in keep]

        if not xs:
            self.draw()
            return

        # Dégradé de couleur du début (bleu) à la fin (rouge), comme l'outil d'origine
        n = len(xs)
        self.ax.scatter(xs, ys, c=range(n), cmap="coolwarm", s=3, linewidths=0)
        self.ax.plot(xs[0], ys[0], "o", color="lime", markersize=8, label="Début")
        self.ax.plot(xs[-1], ys[-1], "o", color="yellow", markersize=8, label="Fin")
        self.ax.legend(facecolor="#222222", labelcolor="white", loc="upper right", fontsize=8)
        self.draw()


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

        # Gauche : trajet
        self.canvas = TrackCanvas()
        splitter.addWidget(self.canvas)

        # Droite : onglets Infos / Logs
        right = QWidget()
        right_layout = QVBoxLayout(right)
        tabs = QTabWidget()
        right_layout.addWidget(tabs)

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
        tabs.addTab(info_box, "Infos")

        # Onglet logs
        logs_widget = QWidget()
        logs_layout = QVBoxLayout(logs_widget)
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
        logs_layout.addWidget(self.list_logs, stretch=1)

        tabs.addTab(logs_widget, "Historique des logs")

        splitter.addWidget(right)
        splitter.setSizes([650, 650])

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Prêt.")

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

        self.statusBar().showMessage(f"{count} lignes affichées sur {len(self.session.lines)}.")


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
