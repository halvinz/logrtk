"""
gui.py — Interface principale : Robot Log Viewer

- Bouton "Ouvrir un dossier de logs" : détecte automatiquement les fichiers
  ..._MODEL.log / ..._pos.log / ..._boot.log / dmesg.txt dans le dossier
  choisi (comme dans l'archive .tar exportée par le robot).
- Carte façon appli officielle : zone tondue en vert plein sur fond clair,
  station de charge en jaune, zones interdites en rouge, chemins vers la
  station en traits noirs et vers une autre zone en vert clair, robot avec
  sa flèche de cap. Molette = zoom, glisser = déplacer, double-clic = vue
  entière, clic = choisir un moment.
- Mode "Show time path" : à partir du moment choisi, Espace fait avancer
  le robot seconde par seconde sur la carte (Maj+Espace pour reculer),
  le chemin parcouru se dessine en bleu et les logs suivent.
"""

from __future__ import annotations

import os
import re
import sys
import math
import bisect
from datetime import datetime, time, timedelta

from PySide6.QtCore import Qt, QEvent, QTimer
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFileDialog, QLineEdit, QComboBox, QListWidget,
    QListWidgetItem, QSplitter, QGroupBox, QFormLayout, QDateTimeEdit,
    QCheckBox, QStatusBar, QMessageBox, QTabWidget, QTextEdit,
    QAbstractItemView
)
from PySide6.QtGui import QColor

import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.colors import ListedColormap

from parser import (load_session_from_folder, load_session,
                    load_session_from_archive, RobotSession, TrackPoint)
from logbible import (analyze_line, analyze_rtk2_line, normalize,
                      robot_categories, search_terms, SEARCH_CATALOG)
from mapmodel import (path_intervals, classify_points, forbidden_zones,
                      extract_state, magnetic_intervals, station_position,
                      station_approach, build_grid, rasterize,
                      zone_timeline, zones_of_points)

# Fenêtre de regroupement des événements identiques dans l'onglet Diagnostic
GROUP_SECONDS = 120


APP_TITLE = "Robot Log Viewer"

# Palette "appli officielle" : fond clair, zone tondue verte
COL_BG = "#f4f4f1"
COL_ZONE = "#1f9e4f"       # tondu sur la période affichée : vert franc
COL_LAWN = "#bfe6ce"       # pelouse entière (tout l'historique) : vert pâle
COL_ZONE_PALE = "#d7efe0"  # les deux estompés en mode analyse
COL_LAWN_PALE = "#ecf6f0"

# Une couleur par zone de tonte, comme dans l'application du robot.
ZONE_COLORS = ["#1f9e4f", "#e8a33d", "#3d7fe8", "#9b59b6", "#16a3a3", "#d4587a"]
COL_TRANSIT = "#f2f2f2"    # couloir de liaison entre deux zones (zone 0)
COL_PATH = "#1565c0"       # chemin parcouru en mode analyse
COL_ROBOT = "#111111"      # position du robot
COL_STATION = "#ffd400"    # station de charge (jaune)
COL_STATION_EDGE = "#111111"
COL_SELECT = "#d81b60"     # anneau de sélection
COL_WAY_STATION = "#111111"  # chemin vers la station : traits noirs
COL_WAY_ZONE = "#7fdc9c"     # chemin vers une autre zone : vert clair
COL_WAY_MAGNET = "#7e57c2"   # suivi de la bande magnétique : violet
COL_UNMOWED = "#dcdcdc"      # poches non tondues à l'intérieur du terrain
COL_ALERT_ERROR = "#c62828"  # repère d'erreur en mode analyse
COL_ALERT_WARN = "#ef8c00"

# Longueur du couloir d'accostage tracé devant la station
DOCK_LINE_METERS = 3.0


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
        self._cats: list[int] = []   # 0 tonte, 1 chemin station, 2 chemin zone
        self._sel_marker = None
        self._trail = None
        self._robot_marker = None
        self._robot_arrow = None
        self._unmowed = None
        self._grid = None
        self._lawn_mask = None
        self._mowed_mask = None
        self._zone_masks: dict = {}
        self._view_pts: list[TrackPoint] = []
        self._station = None
        self._station_heading = None
        self._robot_map = None
        self._show: dict = {}
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
        # "box" (et non "datalim") : matplotlib ajuste le cadre du graphique,
        # jamais les limites — toute la carte reste donc visible.
        self.ax.set_aspect("equal", adjustable="box")
        self._sel_marker = None
        self._trail = None
        self._robot_marker = None
        self._robot_arrow = None

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
        pts = self._view_pts or self._pts
        if not pts:
            return
        xs = [p.x for p in pts]
        ys = [p.y for p in pts]
        if self._station is not None:
            xs.append(self._station[0])
            ys.append(self._station[1])
        if self._robot_map is not None:
            # la carte du robot déborde souvent du trajet : on la cadre entière
            mask, res = self._robot_map
            ys_i, xs_i = np.nonzero(mask)
            if xs_i.size:
                xs += [float(xs_i.min()) * res, float(xs_i.max()) * res]
                ys += [float(ys_i.min()) * res, float(ys_i.max()) * res]
        margin_x = max((max(xs) - min(xs)) * 0.08, 1.0)
        margin_y = max((max(ys) - min(ys)) * 0.08, 1.0)
        self.ax.set_xlim(min(xs) - margin_x, max(xs) + margin_x)
        self.ax.set_ylim(min(ys) - margin_y, max(ys) + margin_y)
        # imshow (zones interdites) peut avoir modifié l'aspect : on le réimpose
        self.ax.set_aspect("equal", adjustable="box")
        self.draw_idle()

    # ------------------------------------------------------------------
    # Aides de tracé
    # ------------------------------------------------------------------
    def _filter_points(self, points, filter_outliers):
        """Écarte les positions aberrantes (sauts SLAM à plusieurs centaines de
        mètres). Le filtre est relatif à l'étendue réelle du terrain : quelques
        points fous suffisaient sinon à réduire tout le jardin à un timbre-poste.
        """
        pts = points
        if not filter_outliers or len(pts) < 50:
            return pts

        xs = sorted(p.x for p in pts)
        ys = sorted(p.y for p in pts)

        def bounds(vals):
            lo = vals[int(len(vals) * 0.01)]
            hi = vals[min(int(len(vals) * 0.99), len(vals) - 1)]
            pad = max((hi - lo) * 0.5, 2.0)   # large marge : on ne coupe pas une zone éloignée
            return lo - pad, hi + pad

        x_lo, x_hi = bounds(xs)
        y_lo, y_hi = bounds(ys)
        kept = [p for p in pts
                if x_lo <= p.x <= x_hi and y_lo <= p.y <= y_hi]
        return kept if kept else pts

    def _runs(self, cat: int):
        """Suites de points consécutifs d'une même catégorie, pour tracer des
        traits continus plutôt qu'un nuage de points."""
        runs, cur = [], []
        for p, c in zip(self._pts, self._cats):
            if c == cat:
                cur.append(p)
            elif cur:
                runs.append(cur)
                cur = []
        if cur:
            runs.append(cur)
        return [r for r in runs if len(r) >= 2]

    def _fill(self, mask, color, alpha, zorder):
        """Peint les cases d'un masque du quadrillage."""
        if mask is None or self._grid is None or not mask.any():
            return
        self.ax.imshow(
            np.ma.masked_where(~mask, mask.astype(float)),
            extent=self._grid.extent, origin="lower", aspect="auto",
            cmap=ListedColormap([color]), alpha=alpha,
            interpolation="nearest", zorder=zorder,
        )

    def _zone_color(self, zone: int) -> str:
        if zone == 0:
            return COL_TRANSIT
        return ZONE_COLORS[(zone - 1) % len(ZONE_COLORS)]

    def _draw_robot_map(self, pale: bool):
        """Carte enregistrée par le robot, dessinée telle quelle en fond.
        Contrairement à la pelouse déduite du trajet, elle donne le contour
        réel du terrain, y compris les parties non parcourues récemment."""
        if self._robot_map is None:
            return False
        mask, res = self._robot_map
        h, w = mask.shape
        self.ax.imshow(
            np.ma.masked_where(~mask, mask.astype(float)),
            extent=(0.0, w * res, 0.0, h * res), origin="lower", aspect="auto",
            cmap=ListedColormap([COL_LAWN_PALE if pale else COL_LAWN]),
            interpolation="nearest", zorder=1,
        )
        return True

    def _draw_zone(self, pale: bool):
        """Pelouse entière en pâle, puis la tonte de la période affichée :
        une couleur par zone comme dans l'application du robot, et le couloir
        de liaison entre zones en clair."""
        has_map = self._draw_robot_map(pale)
        if self._grid is None:
            # trace trop courte pour un quadrillage : on retombe sur des points
            self.ax.scatter([p.x for p in self._pts], [p.y for p in self._pts],
                            color=COL_ZONE_PALE if pale else COL_ZONE,
                            s=14, linewidths=0, zorder=2)
            return
        if not has_map:
            # pas de carte du robot : on retombe sur la pelouse déduite
            self._fill(self._lawn_mask, COL_LAWN_PALE if pale else COL_LAWN,
                       1.0, zorder=1)
        for zone in sorted(self._zone_masks):
            color = COL_ZONE_PALE if pale else self._zone_color(zone)
            # le couloir passe au-dessus : c'est lui qui relie les zones
            self._fill(self._zone_masks[zone], color, 1.0,
                       zorder=3 if zone == 0 else 2)
        self._fill(self._unmowed, COL_UNMOWED, 0.5 if pale else 1.0, zorder=4)

        a = 0.4 if pale else 1.0
        if self._show.get("zone", True):
            for run in self._runs(2):
                self.ax.plot([p.x for p in run], [p.y for p in run],
                             color=COL_WAY_ZONE, linewidth=2.6, alpha=a,
                             solid_capstyle="round", zorder=4)
        if self._show.get("magnet", True):
            for run in self._runs(3):
                self.ax.plot([p.x for p in run], [p.y for p in run],
                             color=COL_WAY_MAGNET, linewidth=2.6, alpha=a,
                             solid_capstyle="round", zorder=5)
        if self._show.get("station", True):
            for run in self._runs(1):
                self.ax.plot([p.x for p in run], [p.y for p in run],
                             color=COL_WAY_STATION, linewidth=1.8,
                             linestyle=(0, (5, 3)), alpha=a, zorder=4)

    def _make_robot_artists(self):
        """Rond du robot + petit triangle de cap posé dessus (odomètre).
        Les deux sont dimensionnés en points écran : la flèche garde la même
        taille quel que soit le zoom."""
        (self._robot_marker,) = self.ax.plot(
            [], [], marker="o", markersize=14, markerfacecolor=COL_ROBOT,
            markeredgecolor="white", markeredgewidth=1.5, linestyle="", zorder=7
        )
        (self._robot_arrow,) = self.ax.plot(
            [], [], marker=(3, 0, 0), markersize=7, markerfacecolor="white",
            markeredgecolor="white", linestyle="", zorder=8
        )

    def _update_robot(self, p: TrackPoint):
        """Place le robot en `p`, triangle orienté selon son cap."""
        if self._robot_marker is None:
            return
        self._robot_marker.set_data([p.x], [p.y])
        if self._robot_arrow is not None:
            # marker=(3, 0, angle) : triangle pointant vers le haut à 0°,
            # tourné dans le sens trigonométrique. Le cap des logs est mesuré
            # depuis l'axe X, d'où le -90°.
            self._robot_arrow.set_marker((3, 0, p.heading - 90))
            self._robot_arrow.set_data([p.x], [p.y])

    def _draw_station(self):
        if self._station is None:
            return
        x, y = self._station
        # Axe d'accostage : le robot rentre en ligne droite face à la station.
        if self._station_heading is not None:
            rad = math.radians(self._station_heading)
            length = DOCK_LINE_METERS
            self.ax.plot([x, x + length * math.cos(rad)],
                         [y, y + length * math.sin(rad)],
                         color=COL_STATION_EDGE, linewidth=2.0,
                         linestyle=(0, (6, 3)), zorder=8)
        self.ax.plot(x, y, marker="o", markersize=17, markerfacecolor=COL_STATION,
                     markeredgecolor=COL_STATION_EDGE, markeredgewidth=2.2,
                     linestyle="", zorder=9)
        self.ax.annotate("⚡", (x, y), ha="center", va="center",
                         fontsize=9, zorder=10)

    def _legend(self, analysis: bool):
        handles = []
        zones = [z for z in sorted(self._zone_masks) if z != 0]
        if len(zones) > 1:
            for z in zones:
                handles.append(Line2D([], [], marker="s", linestyle="",
                                      color=self._zone_color(z),
                                      label=f"Zone {z}"))
            if 0 in self._zone_masks:
                handles.append(Line2D([], [], marker="s", linestyle="",
                                      color=COL_TRANSIT,
                                      label="Liaison entre zones"))
        else:
            handles.append(Line2D([], [], marker="s", linestyle="",
                                  color=COL_ZONE,
                                  label="Tondu sur la période affichée"))
        handles.append(Line2D([], [], marker="s", linestyle="", color=COL_LAWN,
                              label="Pelouse (tout l'historique)"))
        if self._unmowed is not None:
            handles.append(Line2D([], [], marker="s", linestyle="",
                                  color=COL_UNMOWED, label="Non tondu"))
        if self._show.get("station", True) and self._runs(1):
            handles.append(Line2D([], [], color=COL_WAY_STATION,
                                  linestyle=(0, (5, 3)), linewidth=1.8,
                                  label="Chemin vers la station"))
        if self._show.get("magnet", True) and self._runs(3):
            handles.append(Line2D([], [], color=COL_WAY_MAGNET, linewidth=2.6,
                                  label="Suivi bande magnétique"))
        if self._show.get("zone", True) and self._runs(2):
            handles.append(Line2D([], [], color=COL_WAY_ZONE, linewidth=2.6,
                                  label="Chemin vers une autre zone"))
        if self._station is not None:
            handles.append(Line2D([], [], marker="o", linestyle="",
                                  markerfacecolor=COL_STATION,
                                  markeredgecolor=COL_STATION_EDGE,
                                  color=COL_STATION, label="Station de charge"))
            if self._station_heading is not None:
                handles.append(Line2D([], [], color=COL_STATION_EDGE,
                                      linestyle=(0, (6, 3)), linewidth=2.0,
                                      label="Axe d'accostage"))
        handles.append(Line2D([], [], marker="o", linestyle="", color=COL_ROBOT,
                              label="Robot (flèche = sens d'avancement)"))
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
    def plot_track(self, points: list[TrackPoint], filter_outliers: bool = True,
                   station_intervals=None, zone_intervals=None,
                   magnetic_intervals=None, station_xy=None,
                   station_heading=None, show_forbidden: bool = True,
                   show=None, all_points=None, zone_timeline=None,
                   robot_map=None):
        """`points` : la période affichée (vert franc).
        `all_points` : tout l'historique, qui définit l'étendue de la pelouse
        (vert pâle) — c'est ce contraste qui montre ce qui a été tondu."""
        self._reset_axes()
        self._analysis_start = None
        self._show = show or {}
        self._station = station_xy
        self._station_heading = station_heading
        self._robot_map = robot_map

        base = self._filter_points(all_points if all_points else points,
                                   filter_outliers)
        self._grid = build_grid([p.x for p in base], [p.y for p in base])
        self._lawn_mask = None
        self._unmowed = None
        if self._grid is not None:
            self._lawn_mask = rasterize([p.x for p in base],
                                        [p.y for p in base], self._grid)
            if show_forbidden:
                self._unmowed = forbidden_zones(self._lawn_mask, self._grid)

        self._pts = self._filter_points(points, filter_outliers)
        self._ts = [p.ts for p in self._pts]
        self._cats = classify_points(self._pts, station_intervals or [],
                                     zone_intervals or [],
                                     magnetic=magnetic_intervals or [])
        # Un masque par zone de tonte : c'est ce qui donne les couleurs
        # distinctes de l'application officielle.
        zones = zones_of_points(self._pts, zone_timeline or [])
        self._zone_masks = {}
        self._mowed_mask = None
        if self._grid is not None:
            mowed = [(p, z) for p, c, z in zip(self._pts, self._cats, zones)
                     if c == 0]
            for zone in sorted({z for _, z in mowed}):
                pts = [p for p, z in mowed if z == zone]
                self._zone_masks[zone] = rasterize(
                    [p.x for p in pts], [p.y for p in pts], self._grid)
            self._mowed_mask = rasterize([p.x for p, _ in mowed],
                                         [p.y for p, _ in mowed], self._grid)
        # la vue doit englober toute la pelouse, pas seulement le jour affiché
        self._view_pts = base or self._pts
        if not self._pts:
            self.draw()
            return

        self._draw_zone(pale=False)
        self._draw_station()
        (self._sel_marker,) = self.ax.plot(
            [], [], marker="o", markersize=14, markerfacecolor="none",
            markeredgecolor=COL_SELECT, markeredgewidth=2.5, linestyle="",
            zorder=7
        )
        self._make_robot_artists()
        self._legend(analysis=False)
        self.reset_view()
        self._update_robot(self._pts[-1])
        self.draw()

    def coverage(self):
        """(surface tondue sur la période, surface totale) en m², ou None."""
        if self._grid is None or self._lawn_mask is None \
                or self._mowed_mask is None:
            return None
        area = self._grid.cell * self._grid.cell
        return float(self._mowed_mask.sum()) * area, \
            float(self._lawn_mask.sum()) * area

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
    def enter_analysis(self, start: datetime, alerts=None):
        """Passe la carte en mode analyse : zone estompée, le chemin du robot
        se dessinera en bleu à partir de `start` (le zoom courant est gardé).

        `alerts` : liste de (date, gravité) marquées à l'endroit où le robot
        se trouvait. À la vitesse de répétition du clavier, la bannière seule
        défilerait trop vite : ces repères montrent où regarder.
        """
        if not self._pts:
            return
        xlim, ylim = self.ax.get_xlim(), self.ax.get_ylim()
        self._reset_axes()
        self._analysis_start = start
        self._draw_zone(pale=True)
        self._draw_station()
        self._draw_alerts(alerts)
        (self._trail,) = self.ax.plot([], [], color=COL_PATH, linewidth=2.5,
                                      solid_capstyle="round", zorder=5)
        self._make_robot_artists()
        self._legend(analysis=True)
        self.ax.set_xlim(xlim)
        self.ax.set_ylim(ylim)
        self.draw()

    def _draw_alerts(self, alerts):
        """Place un repère à l'endroit où chaque incident s'est produit."""
        if not alerts or not self._pts:
            return
        for severity, color, size in (("warn", COL_ALERT_WARN, 60),
                                      ("error", COL_ALERT_ERROR, 90)):
            xs, ys = [], []
            for ts, sev in alerts:
                if sev != severity:
                    continue
                i = bisect.bisect_left(self._ts, ts)
                i = min(max(i, 0), len(self._pts) - 1)
                xs.append(self._pts[i].x)
                ys.append(self._pts[i].y)
            if xs:
                self.ax.scatter(xs, ys, marker="x", c=color, s=size,
                                linewidths=2, zorder=6)

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
        self._update_robot(current)
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
        self._station_intervals: list = []            # trajets vers/depuis la station
        self._zone_intervals: list = []               # transferts vers une autre zone
        self._magnetic_intervals: list = []           # suivi de la bande magnétique
        self._zone_timeline: list = []                # zone de tonte au fil du temps
        self._station_xy = None                       # position réelle de la station
        self._diag_events: list = []                  # diagnostic déjà calculé
        self._alerts: list = []                       # (ts, diag) erreurs et alertes
        self._alert_ts: list = []                     # leurs dates, pour bisect
        self._alerts_seen: int = 0                    # rencontrées depuis le début
        self._play_timer = QTimer(self)
        self._play_timer.timeout.connect(self._on_play_tick)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # --- Barre du haut : ouverture des fichiers ---
        top_bar = QHBoxLayout()
        self.btn_open_folder = QPushButton("Ouvrir un dossier de logs…")
        self.btn_open_folder.clicked.connect(self.on_open_folder)
        self.btn_open_zip = QPushButton("Ouvrir une archive…")
        self.btn_open_zip.setToolTip("Export du robot : .tar.gz (RTK1) ou .zip (RTK2)")
        self.btn_open_zip.clicked.connect(self.on_open_zip)
        self.btn_open_files = QPushButton("Ouvrir des fichiers…")
        self.btn_open_files.clicked.connect(self.on_open_files)
        self.lbl_loaded = QLabel("Aucun fichier chargé.")
        top_bar.addWidget(self.btn_open_folder)
        top_bar.addWidget(self.btn_open_zip)
        top_bar.addWidget(self.btn_open_files)
        top_bar.addWidget(self.lbl_loaded, stretch=1)
        root.addLayout(top_bar)

        # --- Filtre temporel ---
        filter_bar = QHBoxLayout()
        filter_bar.addWidget(QLabel("Jour :"))
        self.combo_day = QComboBox()
        self.combo_day.addItem("Toute la période")
        self.combo_day.currentIndexChanged.connect(self.on_day_changed)
        filter_bar.addWidget(self.combo_day)
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

        # --- Couches affichées sur la carte ---
        layer_bar = QHBoxLayout()
        layer_bar.addWidget(QLabel("Afficher :"))
        self.chk_forbidden = QCheckBox("Non tondu")
        self.chk_forbidden.setToolTip(
            "Poches jamais parcourues à l'intérieur du terrain : obstacle "
            "(massif, arbre) ou simplement zone pas encore tondue."
        )
        self.chk_way_station = QCheckBox("Chemin station")
        self.chk_way_magnet = QCheckBox("Bande magnétique")
        self.chk_way_zone = QCheckBox("Chemin entre zones")
        self.chk_robot_map = QCheckBox("Carte du robot")
        self.chk_robot_map.setToolTip(
            "Contour réel enregistré par le robot, quand l'export le contient.\n"
            "Décochez pour revenir au terrain déduit des déplacements."
        )
        for chk in (self.chk_robot_map, self.chk_forbidden, self.chk_way_station,
                    self.chk_way_magnet, self.chk_way_zone):
            chk.setChecked(True)
            chk.stateChanged.connect(self.refresh_track)
            layer_bar.addWidget(chk)
        layer_bar.addStretch(1)
        root.addLayout(layer_bar)

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
        self.btn_play = QPushButton("▶ Lecture")
        self.btn_play.setCheckable(True)
        self.btn_play.setToolTip("Fait défiler tout seul, sans maintenir Espace")
        self.btn_play.toggled.connect(self.on_play_toggled)
        analysis_bar.addWidget(self.btn_play)
        self.combo_speed = QComboBox()
        for label, mult in (("×1", 1), ("×2", 2), ("×5", 5), ("×10", 10)):
            self.combo_speed.addItem(label, mult)
        self.combo_speed.setToolTip("Vitesse de lecture (secondes de log par seconde réelle)")
        self.combo_speed.currentIndexChanged.connect(self.on_speed_changed)
        analysis_bar.addWidget(self.combo_speed)
        self.lbl_time = QLabel("")
        self.lbl_time.setStyleSheet(
            "font-size: 17px; font-weight: bold; color: #1565c0; padding-left: 10px;"
        )
        analysis_bar.addWidget(self.lbl_time, stretch=1)
        left_layout.addLayout(analysis_bar)

        # Bannière d'alerte : s'allume quand la lecture franchit une erreur.
        # Elle n'interrompt jamais le défilement — maintenir Espace continue —
        # et reste affichée jusqu'à l'incident suivant, sinon elle serait
        # illisible à la vitesse de répétition du clavier.
        self.lbl_alert = QLabel("")
        self.lbl_alert.setWordWrap(True)
        self.lbl_alert.setVisible(False)
        left_layout.addWidget(self.lbl_alert)

        hint = QLabel(
            "1. Clic sur la carte : choisir un moment  •  2. « Show time path », puis "
            "« Lecture » pour dérouler tout seul (×1 à ×10) ou Espace pour avancer "
            "seconde par seconde (Maj+Espace : reculer)\n"
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

        # Onglet diagnostic (Log bible)
        self.diag_widget = QWidget()
        diag_layout = QVBoxLayout(self.diag_widget)
        search_diag = QHBoxLayout()
        self.txt_diag_search = QLineEdit()
        self.txt_diag_search.setPlaceholderText(
            "Rechercher une panne : évite un piège, patine, choc, pluie, rtk…"
        )
        self.txt_diag_search.textChanged.connect(self.display_diagnostic)
        search_diag.addWidget(self.txt_diag_search, stretch=1)
        btn_help = QPushButton("?")
        btn_help.setMaximumWidth(30)
        btn_help.setToolTip("Que peut-on rechercher ?")
        btn_help.clicked.connect(self.show_search_help)
        search_diag.addWidget(btn_help)
        diag_layout.addLayout(search_diag)

        diag_bar = QHBoxLayout()
        self.chk_diag_errors = QCheckBox("Erreurs et alertes uniquement")
        self.chk_diag_errors.setChecked(True)
        self.chk_diag_errors.stateChanged.connect(self.display_diagnostic)
        diag_bar.addWidget(self.chk_diag_errors)
        self.chk_diag_group = QCheckBox("Regrouper les répétitions")
        self.chk_diag_group.setChecked(True)
        self.chk_diag_group.setToolTip(
            "Une ligne par problème, avec le nombre d'occurrences et la période.\n"
            "Décochez pour retrouver l'ordre chronologique des incidents."
        )
        self.chk_diag_group.stateChanged.connect(self.display_diagnostic)
        diag_bar.addWidget(self.chk_diag_group)
        self.lbl_diag = QLabel("")
        diag_bar.addWidget(self.lbl_diag, stretch=1)
        diag_layout.addLayout(diag_bar)
        self.list_diag = QListWidget()
        self.list_diag.setStyleSheet("font-size: 12px;")
        self.list_diag.setWordWrap(True)
        self.list_diag.currentRowChanged.connect(self.on_diag_row_changed)
        diag_layout.addWidget(self.list_diag, stretch=1)
        diag_hint = QLabel(
            "Cliquez sur un événement : il est repéré sur la carte — lancez ensuite "
            "« Show time path » pour rejouer ce que faisait le robot à ce moment-là."
        )
        diag_hint.setWordWrap(True)
        diag_hint.setStyleSheet("color: #888888; font-size: 10px;")
        diag_layout.addWidget(diag_hint)
        self.tabs.addTab(self.diag_widget, "Diagnostic")

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
            self.canvas.enter_analysis(start, self._alert_marks())
            self.canvas.show_until(start)
            self.btn_timepath.setText("⏹  Show time path (actif)")
            self.lbl_time.setText(f"⏱  {start:%d/%m/%Y %H:%M:%S}")
            self.sync_logs_to_time(start)
            self._alerts_seen = 0
            self.lbl_alert.setVisible(False)
            n = len([t for t in self._alert_ts if t >= start])
            self.statusBar().showMessage(
                "Mode analyse : maintenez Espace pour avancer seconde par seconde, "
                f"Maj+Espace pour reculer. {n} incident(s) à venir seront signalés."
            )
            self.setFocus()
        else:
            self.btn_timepath.setText("▶  Show time path")
            self.lbl_time.setText("")
            self.lbl_alert.setVisible(False)
            self.analysis_time = None
            if self.btn_play.isChecked():
                self.btn_play.setChecked(False)
            self.refresh_track()

    # ------------------------------------------------------------------
    # Lecture automatique
    # ------------------------------------------------------------------
    def _speed(self) -> int:
        return self.combo_speed.currentData() or 1

    def on_speed_changed(self):
        if self._play_timer.isActive():
            self._play_timer.setInterval(int(1000 / self._speed()))

    def on_play_toggled(self, playing: bool):
        if playing and not self.btn_timepath.isChecked():
            # la lecture n'a de sens qu'en mode analyse : on l'active
            self.btn_timepath.setChecked(True)
            if not self.btn_timepath.isChecked():   # refusé faute de logs
                self.btn_play.setChecked(False)
                return
        if playing:
            self.btn_play.setText("⏸ Pause")
            self._play_timer.start(int(1000 / self._speed()))
        else:
            self.btn_play.setText("▶ Lecture")
            self._play_timer.stop()

    def _on_play_tick(self):
        """Une seconde de log par battement : la vitesse choisie règle la
        cadence, pas la taille du pas, pour ne franchir aucun incident."""
        if self.analysis_time is None or not self.canvas._pts:
            self.btn_play.setChecked(False)
            return
        if self.analysis_time >= self.canvas._pts[-1].ts:
            self.btn_play.setChecked(False)
            self.statusBar().showMessage("Fin de la période : lecture terminée.")
            return
        self.step_analysis(1)

    def step_analysis(self, seconds: int):
        if self.analysis_time is None or not self.canvas._pts:
            return
        previous = self.analysis_time
        t = self.analysis_time + timedelta(seconds=seconds)
        t_min = self.analysis_start
        t_max = self.canvas._pts[-1].ts
        t = max(t_min, min(t, t_max))
        self.analysis_time = t
        self.canvas.show_until(t)
        self.lbl_time.setText(f"⏱  {t:%d/%m/%Y %H:%M:%S}")
        self.sync_logs_to_time(t)
        if t != previous:
            self._show_alert(self._alerts_between(previous, t))

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
            self.canvas.enter_analysis(point.ts, self._alert_marks())
            self.canvas.show_until(point.ts)
            self.lbl_time.setText(f"⏱  {point.ts:%d/%m/%Y %H:%M:%S}")
            self.sync_logs_to_time(point.ts)
            self._alerts_seen = 0
            self.lbl_alert.setVisible(False)
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

    def on_open_zip(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choisir une archive de logs", "",
            "Archives de logs (*.zip *.tar.gz *.tgz *.tar);;Tous les fichiers (*)"
        )
        if not path:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.session = load_session_from_archive(path)
        except Exception as e:
            QMessageBox.critical(self, "Erreur", f"Impossible de lire cette archive :\n{e}")
            return
        finally:
            QApplication.restoreOverrideCursor()
        self._after_load(os.path.basename(path))

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

        # Seulement ce qui a été ouvert : le détail des journaux lus encombrait
        # la barre. Il reste consultable en infobulle.
        self.lbl_loaded.setText(f"Chargé : {source_label}")
        self.lbl_loaded.setToolTip(
            f"{len(s.files_loaded)} journaux lus :\n" + "\n".join(sorted(s.files_loaded))
        )

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

        self._station_intervals, self._zone_intervals = path_intervals(s.lines)
        self._magnetic_intervals = magnetic_intervals(s.lines)
        self._zone_timeline = zone_timeline(s.lines)
        # en RTK2 la station et son axe sont donnés par les logs ; en RTK1 on
        # les déduit des passages en charge
        self._station_xy = s.station_xy or station_position(s.track, s.lines)
        self._station_heading = s.station_heading
        if self._station_heading is None:
            self._station_heading = station_approach(s.track, s.lines,
                                                     self._station_xy)

        # Liste des journées présentes dans la trace
        days = sorted({p.ts.date() for p in s.track if p.ts.year >= 2020})
        self.combo_day.blockSignals(True)
        self.combo_day.clear()
        self.combo_day.addItem("Toute la période")
        for d in days:
            self.combo_day.addItem(d.strftime("%d/%m/%Y"), d)
        self.combo_day.blockSignals(False)

        self.statusBar().showMessage(
            f"{len(s.track)} points de position — {len(s.lines)} lignes de log."
        )

        self.refresh_view()
        self._warn_if_no_track()

    def _warn_if_no_track(self):
        """Sans position, la carte reste vide : mieux vaut dire pourquoi que
        laisser croire à un défaut du logiciel."""
        if not self.session or self.session.track:
            return
        cause = ""
        # le résumé classe déjà les pannes du robot en tête, par gravité
        for _ts, _end, d, count, _raw in self._summarize(self._diag_events):
            if d["severity"] == "error":
                cause = f"\n\nPiste la plus probable :\n{d['meaning']} ({count} fois)"
                if d["conclusion"]:
                    cause += f"\n→ {d['conclusion']}"
                break
        QMessageBox.information(
            self, "Aucune position dans ces logs",
            "Le robot n'a enregistré aucune position sur cette période : "
            "la carte reste donc vide.\n\nL'onglet Diagnostic reste "
            "exploitable et contient les erreurs remontées par le robot."
            + cause
        )

    # ------------------------------------------------------------------
    # Rafraîchissement
    # ------------------------------------------------------------------
    def on_day_changed(self, index: int):
        """Choix d'une journée : cale les bornes sur ce jour-là pour ne voir
        que ce que le robot a tondu ce jour, ou revient à toute la période."""
        if not self.session:
            return
        day = self.combo_day.itemData(index)
        if day is None:
            valid = [p.ts for p in self.session.track if p.ts.year >= 2020]
            if valid:
                self.dt_start.setDateTime(min(valid))
                self.dt_end.setDateTime(max(valid))
        else:
            self.dt_start.setDateTime(datetime.combine(day, time.min))
            self.dt_end.setDateTime(datetime.combine(day, time.max))
        self.refresh_view()

    def refresh_view(self):
        self.refresh_track()
        self.refresh_log_list()
        self.refresh_diagnostic()

    def refresh_diagnostic(self):
        """Recalcule les événements puis rafraîchit l'affichage. Coûteux :
        n'est appelé qu'au chargement ou quand un filtre change, jamais à
        chaque frappe dans la recherche (les logs RTK2 font 800 000 lignes)."""
        if not self.session:
            return
        start = self.dt_start.dateTime().toPython()
        end = self.dt_end.dateTime().toPython()

        # Une rafale du même problème (chocs, patinage, 4G…) devient une seule
        # ligne « ×N, de hh:mm:ss à hh:mm:ss » : sinon l'onglet est illisible.
        BURST = timedelta(seconds=GROUP_SECONDS)
        events = []  # [ts_debut, ts_fin, diag, count, raw]
        state = None
        for l in self.session.lines:
            if l.ts is None:
                continue
            found = extract_state(l.raw)
            if found:
                state = found
            if not (start <= l.ts <= end):
                continue
            diag = analyze_line(l.raw, state)
            if diag is None and self.session.fmt == "rtk2":
                # pas de bible pour ce modèle : on se fie à la gravité que
                # le robot inscrit lui-même dans ses journaux
                diag = analyze_rtk2_line(l.level, l.tag, l.text)
            if diag is None:
                continue
            # On regroupe sur le libellé exact (et non la seule catégorie) :
            # sinon une cause rare — surchauffe moteur, roue bloquée — serait
            # avalée par la rafale voisine et deviendrait introuvable.
            if events and events[-1][2]["meaning"] == diag["meaning"] \
                    and (l.ts - events[-1][1]) <= BURST:
                events[-1][1] = l.ts
                events[-1][3] += 1
            else:
                events.append([l.ts, l.ts, diag, 1, l.raw])

        self._diag_events = events
        # incidents à signaler pendant la lecture « Show time path »
        self._alerts = [(e[0], e[2]) for e in events
                        if e[2]["severity"] in ("error", "warn")]
        self._alert_ts = [a[0] for a in self._alerts]
        self.display_diagnostic()

    # ------------------------------------------------------------------
    # Alertes pendant la lecture
    # ------------------------------------------------------------------
    def _alert_marks(self) -> list:
        """(date, gravité) des incidents, pour les repérer sur la carte."""
        return [(ts, d["severity"]) for ts, d in self._alerts]

    def _alerts_between(self, t0: datetime, t1: datetime) -> list:
        """Incidents franchis en passant de `t0` à `t1`. L'intervalle est
        semi-ouvert du côté déjà parcouru : sans cela, un incident tombant
        pile sur une seconde serait signalé deux fois de suite."""
        if t1 > t0:                       # avance : (t0, t1]
            i = bisect.bisect_right(self._alert_ts, t0)
            j = bisect.bisect_right(self._alert_ts, t1)
        else:                             # recul : [t1, t0)
            i = bisect.bisect_left(self._alert_ts, t1)
            j = bisect.bisect_left(self._alert_ts, t0)
        return self._alerts[i:j]

    def _show_alert(self, hits: list):
        """Affiche l'incident le plus grave parmi ceux qui viennent d'être
        franchis. Purement visuel : aucune boîte de dialogue, sinon le
        défilement à touche maintenue serait interrompu à chaque erreur."""
        if not hits:
            return
        self._alerts_seen += len(hits)
        ts, d = min(hits, key=lambda h: 0 if h[1]["severity"] == "error" else 1)
        error = d["severity"] == "error"
        icon = "⛔" if error else "⚠"
        text = f"{icon}  {ts:%d/%m %H:%M:%S}   [{d['category']}]  {d['meaning']}"
        if d["conclusion"]:
            text += f"\n     → {d['conclusion']}"
        if len(hits) > 1:
            text += f"     (+{len(hits) - 1} autre(s) au même moment)"
        text += f"\n     {self._alerts_seen} incident(s) depuis le début de la lecture"
        self.lbl_alert.setText(text)
        self.lbl_alert.setStyleSheet(
            "background: %s; color: white; font-size: 12px; font-weight: bold;"
            " padding: 6px; border-radius: 4px;"
            % ("#c62828" if error else "#ef8c00")
        )
        self.lbl_alert.setVisible(True)

    def _summarize(self, events):
        """Rassemble toutes les occurrences d'un même problème en une ligne.
        Les nombres du message sont ignorés dans la comparaison : sans cela
        « region id[1] » et « region id[5] » comptent pour deux problèmes
        différents et l'onglet déborde (11 000 lignes sur un export RTK2)."""
        merged = {}
        for ts, ts_end, d, count, raw in events:
            # Le message est tronqué en amont : deux variantes du même
            # problème peuvent donc se terminer différemment. On ne compare
            # qu'un début stable, chiffres neutralisés.
            sig = (d["severity"], d["category"],
                   re.sub(r"\d+", "N", d["meaning"])[:80])
            hit = merged.get(sig)
            if hit is None:
                merged[sig] = [ts, ts_end, d, count, raw]
            else:
                hit[0] = min(hit[0], ts)
                hit[1] = max(hit[1], ts_end)
                hit[3] += count
        order = {"error": 0, "warn": 1, "info": 2}
        robot = robot_categories()
        # les pannes de la tondeuse d'abord, le bruit logiciel ensuite
        return sorted(merged.values(),
                      key=lambda e: (0 if e[2]["category"] in robot else 1,
                                     order.get(e[2]["severity"], 3), -e[3]))

    def display_diagnostic(self):
        """Affiche les événements déjà calculés, selon la recherche et le
        mode de regroupement choisis."""
        if not self.session:
            return
        events = self._diag_events
        n_err = sum(1 for e in events if e[2]["severity"] == "error")
        n_warn = sum(1 for e in events if e[2]["severity"] == "warn")
        if self.chk_diag_group.isChecked():
            events = self._summarize(events)

        # Recherche de panne : tous les mots tapés doivent se retrouver dans la
        # description, la solution, les synonymes français ou la ligne de log.
        words = search_terms(self.txt_diag_search.text())

        only_problems = self.chk_diag_errors.isChecked()
        self.list_diag.blockSignals(True)
        self.list_diag.clear()
        shown = 0
        for ts, ts_end, d, count, raw in events:
            if words:
                hay = normalize(" ".join((d["category"], d["meaning"],
                                          d["conclusion"], d["keys"], raw)))
                if not all(w in hay for w in words):
                    continue
            elif only_problems and d["severity"] == "info":
                continue
            text = f"{ts:%d/%m %H:%M:%S}   [{d['category']}]  {d['meaning']}"
            if count > 1:
                fin = (f"{ts_end:%H:%M:%S}" if ts_end.date() == ts.date()
                       else f"{ts_end:%d/%m %H:%M:%S}")
                text += f"  (×{count} jusqu'à {fin})"
            if d["conclusion"]:
                text += f"\n        → {d['conclusion']}"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, ts)
            if d["severity"] == "error":
                item.setForeground(QColor("#c62828"))
            elif d["severity"] == "warn":
                item.setForeground(QColor("#b36b00"))
            else:
                item.setForeground(QColor("#555555"))
            self.list_diag.addItem(item)
            shown += 1
        self.list_diag.blockSignals(False)

        if words:
            if shown:
                self.lbl_diag.setText(f"{shown} événement(s) trouvé(s) pour cette recherche")
            else:
                self.lbl_diag.setText(
                    "Aucun événement de ce type dans ces logs (période affichée)"
                )
        else:
            self.lbl_diag.setText(
                f"{n_err} erreur(s), {n_warn} alerte(s) — {shown} événement(s) affiché(s)"
            )
        self.tabs.setTabText(self.tabs.indexOf(self.diag_widget),
                             f"Diagnostic ({n_err + n_warn})" if (n_err + n_warn) else "Diagnostic")

    def show_search_help(self):
        lines = ["Tapez ce que vous cherchez en français, le logiciel fait le lien",
                 "avec les messages anglais des logs.", ""]
        for titre, mots in SEARCH_CATALOG:
            lines.append(f"• {titre} : {mots}")
        lines += ["", "Vous pouvez aussi taper le texte anglais du log "
                      "(ex. « Escaping from trap »),",
                  "ou plusieurs mots à la fois (ex. « moteur surchauffe »)."]
        QMessageBox.information(self, "Que peut-on rechercher ?", "\n".join(lines))

    def on_diag_row_changed(self, row: int):
        """Clic sur un événement du diagnostic : repère le moment sur la carte."""
        if row < 0 or not self.session:
            return
        item = self.list_diag.item(row)
        ts = item.data(Qt.UserRole) if item else None
        if ts is None:
            return
        self.analysis_start = ts
        if self.btn_timepath.isChecked():
            self.analysis_time = ts
            self.canvas.enter_analysis(ts, self._alert_marks())
            self.canvas.show_until(ts)
            self.lbl_time.setText(f"⏱  {ts:%d/%m/%Y %H:%M:%S}")
            self._alerts_seen = 0
            self.lbl_alert.setVisible(False)
        else:
            self.canvas.select_time(ts)
        self.sync_logs_to_time(ts)
        self.statusBar().showMessage(
            f"Événement du {ts:%d/%m/%Y %H:%M:%S} repéré sur la carte — "
            "« Show time path » pour rejouer le comportement."
        )

    def refresh_track(self):
        if not self.session:
            return
        start = self.dt_start.dateTime().toPython()
        end = self.dt_end.dateTime().toPython()
        pts = [p for p in self.session.track if start <= p.ts <= end]
        # tout l'historique valide : il dessine la pelouse en vert pâle,
        # sur laquelle se détache la tonte de la période choisie
        history = [p for p in self.session.track if p.ts.year >= 2020]
        self.canvas.plot_track(
            pts,
            all_points=history,
            filter_outliers=self.chk_outliers.isChecked(),
            station_intervals=self._station_intervals,
            zone_intervals=self._zone_intervals,
            magnetic_intervals=self._magnetic_intervals,
            station_xy=self._station_xy,
            station_heading=self._station_heading,
            show_forbidden=self.chk_forbidden.isChecked(),
            zone_timeline=self._zone_timeline,
            robot_map=self.session.robot_map if self.chk_robot_map.isChecked() else None,
            show={
                "station": self.chk_way_station.isChecked(),
                "magnet": self.chk_way_magnet.isChecked(),
                "zone": self.chk_way_zone.isChecked(),
            },
        )
        cov = self.canvas.coverage()
        if cov:
            mowed, lawn = cov
            pct = 100 * mowed / lawn if lawn else 0
            self.statusBar().showMessage(
                f"Tonte affichée : {mowed:.0f} m² sur {lawn:.0f} m² de pelouse "
                f"({pct:.0f} %) — {len(pts)} positions."
            )

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
