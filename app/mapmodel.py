"""
mapmodel.py — Reconstruction de la carte à partir des logs.

Deux choses que les logs ne donnent pas directement mais qu'on peut déduire :

1. Les zones interdites (îlots) : le robot ne roule jamais dedans. On
   quadrille le terrain, on marque les cases visitées, puis on cherche les
   poches jamais visitées mais complètement entourées de zone tondue.
   Ce sont les massifs, arbres, piscines… (les « island » des logs).

2. Les chemins : portions de trajet où le robot ne tond pas mais se
   déplace — retour/départ station (états PreStart, LeaveBase, Charge,
   Return) et transfert vers une autre zone (channel trans).
"""

from __future__ import annotations

import re
import math
import bisect
from collections import deque
from datetime import datetime, timedelta

import numpy as np


# Le RTC du robot repart en 2017 après certaines coupures : dates à ignorer
MIN_VALID_YEAR = 2020

_RE_STATE = re.compile(r"Ui_Inter_(Sub_)?WorkMode_(\w+)", re.I)
_RE_RTK2_STATE = re.compile(r"\[(Work|Charge) State\]\s*\[\d+\]\s*(Enter|Exit)\s+(.+)", re.I)

# États pendant lesquels le robot circule au lieu de tondre
DEPART_STATES = {"prestart", "leavebase", "wait_leavebase_result", "return"}
ARRIVE_STATES = {"charge", "checkcharge", "precheckinstation", "docking"}
MOWING_STATES = {"start"}

# Un trajet station <-> zone dure quelques minutes : au-delà, c'est que le
# robot stationne (nuit de charge) et non qu'il roule.
MAX_TRANSIT_SECONDS = 900


def extract_state(raw: str) -> str | None:
    """État du robot déduit d'une ligne de log, sinon None.

    Deux machines à états selon la génération :
      RTK1  *Ui_Inter_WorkMode_Error# --> *Ui_Inter_WorkMode_PrePowerOff#
      RTK2  [Work State] [12] Enter Auto   /   [Charge State] [18] Exit Charge Doing
    Elles sont ramenées au même vocabulaire pour que la détection des
    trajets fonctionne à l'identique sur les deux formats.
    """
    low = raw.lower()
    if "workmode" in low:
        found = [name for _sub, name in _RE_STATE.findall(raw)
                 if name.lower() != "change"]
        return found[-1].lower() if found else None

    m = _RE_RTK2_STATE.search(raw)
    if not m:
        return None
    kind, action, name = m.group(1).lower(), m.group(2).lower(), m.group(3)
    name = name.strip().lower().replace(" ", "")
    if kind == "charge":
        if name.startswith("chargedoing"):
            # entrée en charge = arrivée à la station, sortie = départ
            return "charge" if action == "enter" else "prestart"
        return None
    if action != "enter":
        return None
    return {"auto": "start", "idle": "idle", "poweroff": "prepoweroff",
            "manualriding": "idle"}.get(name)


def state_timeline(lines) -> list:
    """[(ts, state_bas_de_casse, is_sub)] à partir des lignes de log."""
    events = []
    for l in lines:
        if l.ts is None or l.ts.year < MIN_VALID_YEAR:
            continue
        state = extract_state(l.raw)
        if state:
            events.append((l.ts, state, "sub_workmode" in l.raw.lower()))
    events.sort(key=lambda e: e[0])
    return events


def _merge(intervals, gap_seconds=90):
    """Fusionne les intervalles qui se chevauchent ou se suivent de près."""
    if not intervals:
        return []
    intervals = sorted(intervals)
    out = [list(intervals[0])]
    for a, b in intervals[1:]:
        if (a - out[-1][1]).total_seconds() <= gap_seconds:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [tuple(x) for x in out]


def path_intervals(lines) -> tuple:
    """Renvoie (chemins_station, chemins_zone) : deux listes d'intervalles
    (début, fin) pendant lesquels le robot circule au lieu de tondre."""
    events = state_timeline(lines)
    station, zone = [], []

    # --- chemins station -------------------------------------------------
    # Départ : de PreStart/LeaveBase jusqu'au début de la tonte.
    # Retour : la fenêtre qui précède immédiatement l'arrivée en station.
    # Les fenêtres sont plafonnées : sinon une nuit entière passée à charger
    # serait comptée comme un trajet.
    cap = timedelta(seconds=MAX_TRANSIT_SECONDS)
    open_ts = None
    prev_ts = None
    for ts, state, _sub in events:
        depart = any(state.startswith(s) for s in DEPART_STATES)
        arrive = any(state.startswith(s) for s in ARRIVE_STATES)
        ends = any(state.startswith(s) for s in MOWING_STATES) \
            or state.startswith("idle") or state.startswith("prepoweroff")

        if depart:
            if open_ts is None:
                open_ts = ts
        elif arrive:
            begin = open_ts if open_ts is not None else (prev_ts or ts)
            begin = max(begin, ts - cap)
            if begin < ts:
                station.append((begin, ts))
            open_ts = None
        elif ends and open_ts is not None:
            station.append((open_ts, min(ts, open_ts + cap)))
            open_ts = None
        prev_ts = ts
    if open_ts is not None and prev_ts is not None and open_ts < prev_ts:
        station.append((open_ts, min(prev_ts, open_ts + cap)))

    # --- chemins vers une autre zone : passages par un couloir (channel trans)
    zone_hits = []
    for l in lines:
        if l.ts is None or l.ts.year < MIN_VALID_YEAR:
            continue
        low = l.raw.lower()
        if "channel trans = true" in low or "move_zone" in low \
                or "change zone" in low or "subarea_change" in low:
            zone_hits.append((l.ts, l.ts))
    zone = _merge(zone_hits, gap_seconds=30)

    return _merge(station), zone


def magnetic_intervals(lines) -> list:
    """Périodes pendant lesquelles le robot suit la bande magnétique
    (sortie de base et guidage retour)."""
    cap = timedelta(seconds=MAX_TRANSIT_SECONDS)
    out = []
    open_ts = None
    for l in lines:
        if l.ts is None or l.ts.year < MIN_VALID_YEAR:
            continue
        low = l.raw.lower()
        if "magnet" not in low:
            continue
        starts = ("magneticstart" in low
                  or "startfollowstripe" in low
                  or "setfollowmagneticstate => [true]" in low)
        stops = ("magneticstop" in low
                 or "setfollowmagneticstate => [false]" in low
                 or "magnet_guidance_work_mode change from" in low
                 and ("--> *stop#" in low or "--> *idle#" in low))
        if starts and open_ts is None:
            open_ts = l.ts
        elif stops and open_ts is not None:
            out.append((open_ts, min(l.ts, open_ts + cap)))
            open_ts = None
    return _merge(out, gap_seconds=10)


def station_position(points, lines):
    """Position réelle de la station de charge : là où se trouve le robot
    quand il passe en charge / en accostage. L'origine (0,0) de la carte SLAM
    n'est PAS la station — la supposer donnait un repère au milieu du vide.

    Renvoie (x, y) ou None si les logs ne permettent pas de la situer.
    """
    if not points:
        return None
    times = [p.ts for p in points]
    hits = []
    for l in lines:
        if l.ts is None or l.ts.year < MIN_VALID_YEAR:
            continue
        state = extract_state(l.raw)
        if state and any(state.startswith(s) for s in ARRIVE_STATES):
            i = bisect.bisect_left(times, l.ts)
            if 0 <= i < len(points):
                hits.append(points[i])
    if not hits:
        return None
    xs = sorted(p.x for p in hits)
    ys = sorted(p.y for p in hits)
    return xs[len(xs) // 2], ys[len(ys) // 2]


_RE_ZONE = re.compile(
    r"CURRENT_REGION_ID:\s*\[(\d+)\]|region[ _]id\s*=\s*(\d+)", re.I)


def zone_timeline(lines) -> list:
    """[(date, numéro de zone)] au fil du temps.

    Les robots multi-zones indiquent la zone en cours de tonte ; la zone 0
    correspond aux moments où le robot n'est dans aucune zone, c'est-à-dire
    quand il emprunte le couloir de liaison entre deux zones.
    """
    out = []
    for l in lines:
        if l.ts is None or l.ts.year < MIN_VALID_YEAR:
            continue
        m = _RE_ZONE.search(l.raw)
        if not m:
            continue
        zone = int(m.group(1) or m.group(2))
        if not out or out[-1][1] != zone:
            out.append((l.ts, zone))
    out.sort(key=lambda e: e[0])
    return out


def zones_of_points(points, timeline) -> list:
    """Zone de chaque position, d'après la dernière zone annoncée avant elle."""
    if not timeline:
        return [1] * len(points)
    times = [t for t, _ in timeline]
    zones = [z for _, z in timeline]
    out = []
    for p in points:
        i = bisect.bisect_right(times, p.ts) - 1
        out.append(zones[i] if i >= 0 else 0)
    return out


def station_approach(points, lines, station, max_dist=8.0):
    """Cap de l'axe d'accostage quand les logs ne le donnent pas (RTK1) :
    on relève par où le robot arrive dans les secondes qui précèdent la mise
    en charge. Renvoie un cap en degrés, ou None si on manque de passages.
    """
    if not points or not station:
        return None
    sx, sy = station
    times = [p.ts for p in points]
    bearings = []
    for l in lines:
        if l.ts is None or l.ts.year < MIN_VALID_YEAR:
            continue
        state = extract_state(l.raw)
        if not state or not any(state.startswith(s) for s in ARRIVE_STATES):
            continue
        for back in (5, 10, 20):
            i = bisect.bisect_left(times, l.ts - timedelta(seconds=back))
            if not (0 <= i < len(points)):
                continue
            p = points[i]
            dist = math.hypot(p.x - sx, p.y - sy)
            if 0.5 < dist < max_dist:
                bearings.append(math.atan2(p.y - sy, p.x - sx))
    if len(bearings) < 2:
        return None

    def circular_mean(values):
        # une moyenne arithmétique se tromperait de tout autour de ±180°
        mx = sum(math.cos(b) for b in values) / len(values)
        my = sum(math.sin(b) for b in values) / len(values)
        return None if (mx == 0 and my == 0) else math.atan2(my, mx)

    mean = circular_mean(bearings)
    if mean is None:
        return None
    # Certaines approches partent de travers (le robot manœuvre encore) :
    # on écarte celles qui s'éloignent trop, puis on refait la moyenne.
    close = [b for b in bearings
             if abs(math.degrees(math.atan2(math.sin(b - mean),
                                            math.cos(b - mean)))) <= 45]
    if len(close) >= 2:
        mean = circular_mean(close) or mean
    return math.degrees(mean)


def classify_points(points, station_intervals, zone_intervals,
                    magnetic=None, min_move=0.12) -> list:
    """0 = tonte, 1 = chemin station, 2 = chemin vers une autre zone,
    3 = suivi de la bande magnétique.

    Un point n'est classé « chemin » que si le robot bouge réellement à cet
    instant : sinon le robot immobile sur sa base dessinerait un paquet de
    traits au même endroit.
    """
    cats = []
    n = len(points)
    for i, p in enumerate(points):
        c = 0
        for a, b in (magnetic or []):
            if a <= p.ts <= b:
                c = 3
                break
        if c == 0:
            for a, b in zone_intervals:
                if a <= p.ts <= b:
                    c = 2
                    break
        if c == 0:
            for a, b in station_intervals:
                if a <= p.ts <= b:
                    c = 1
                    break
        if c != 0:
            j = min(i + 2, n - 1)
            k = max(i - 2, 0)
            moved = ((points[j].x - points[k].x) ** 2
                     + (points[j].y - points[k].y) ** 2) ** 0.5
            if moved < min_move:
                c = 0
        cats.append(c)
    return cats


class Grid:
    """Quadrillage du terrain, partagé par toutes les couches de la carte
    pour qu'elles se superposent exactement."""

    def __init__(self, x0, y0, cell, nx, ny):
        self.x0, self.y0, self.cell, self.nx, self.ny = x0, y0, cell, nx, ny
        # imshow répartit nx cases sur [left, right] : on cale les bords pour
        # que le centre de la case j retombe sur x0 + (j-1) * cell.
        self.extent = (x0 - 1.5 * cell, x0 + (nx - 1.5) * cell,
                       y0 - 1.5 * cell, y0 + (ny - 1.5) * cell)


def build_grid(xs, ys, max_grid=260, min_cell=0.35):
    """Quadrillage couvrant tout le terrain. La taille de case ne descend pas
    sous ~35 cm : c'est la largeur du robot, en dessous les passages voisins
    ne se rejoindraient plus et la pelouse apparaîtrait pleine de trous."""
    if len(xs) < 50:
        return None
    x0, x1 = float(min(xs)), float(max(xs))
    y0, y1 = float(min(ys)), float(max(ys))
    span = max(x1 - x0, y1 - y0)
    if span <= 0:
        return None
    cell = max(span / max_grid, min_cell)
    nx = int((x1 - x0) / cell) + 3
    ny = int((y1 - y0) / cell) + 3
    if nx < 8 or ny < 8:
        return None
    return Grid(x0, y0, cell, nx, ny)


def rasterize(xs, ys, grid: Grid, dilate: bool = True):
    """Cases du quadrillage parcourues par le robot, épaissies d'une case
    (largeur de la machine) pour obtenir une surface pleine."""
    mask = np.zeros((grid.ny, grid.nx), dtype=bool)
    if len(xs) == 0:
        return mask
    ix = ((np.asarray(xs, dtype=float) - grid.x0) / grid.cell).astype(int) + 1
    iy = ((np.asarray(ys, dtype=float) - grid.y0) / grid.cell).astype(int) + 1
    ok = (ix >= 0) & (ix < grid.nx) & (iy >= 0) & (iy < grid.ny)
    mask[iy[ok], ix[ok]] = True
    if not dilate:
        return mask
    grown = mask.copy()
    grown[1:, :] |= mask[:-1, :]
    grown[:-1, :] |= mask[1:, :]
    grown[:, 1:] |= mask[:, :-1]
    grown[:, :-1] |= mask[:, 1:]
    return grown


def forbidden_zones(covered, grid: Grid, min_area_m2=2.0):
    """Poches jamais visitées mais entourées de pelouse : ce sont les zones
    interdites / obstacles (les « island » des logs).

    `covered` est le masque de la surface parcourue sur tout l'historique.
    Renvoie un masque booléen, ou None s'il n'y a rien de significatif.
    """
    if covered is None or grid is None:
        return None
    ny, nx = covered.shape
    free = ~covered
    outside = np.zeros_like(free)
    q = deque()
    for i in range(ny):
        for j in (0, nx - 1):
            if free[i, j] and not outside[i, j]:
                outside[i, j] = True
                q.append((i, j))
    for j in range(nx):
        for i in (0, ny - 1):
            if free[i, j] and not outside[i, j]:
                outside[i, j] = True
                q.append((i, j))
    while q:
        i, j = q.popleft()
        for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            a, b = i + di, j + dj
            if 0 <= a < ny and 0 <= b < nx and free[a, b] and not outside[a, b]:
                outside[a, b] = True
                q.append((a, b))

    holes = free & ~outside
    if holes.sum() < 6:      # rien de significatif
        return None

    # Une vraie zone interdite (arbre, massif, bassin) fait au moins ~2 m² ;
    # en dessous c'est un simple coin oublié par la tondeuse, et l'afficher
    # en rouge brouillerait la lecture de la carte.
    min_size = max(5, int(min_area_m2 / (grid.cell * grid.cell)))
    return _drop_small(holes, min_size=min_size)


def _drop_small(mask, min_size):
    """Retire les composantes connexes de moins de `min_size` cases."""
    ny, nx = mask.shape
    seen = np.zeros_like(mask)
    out = np.zeros_like(mask)
    for i in range(ny):
        for j in range(nx):
            if not mask[i, j] or seen[i, j]:
                continue
            comp = []
            q = deque([(i, j)])
            seen[i, j] = True
            while q:
                a, b = q.popleft()
                comp.append((a, b))
                for da, db in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    c, d = a + da, b + db
                    if 0 <= c < ny and 0 <= d < nx and mask[c, d] and not seen[c, d]:
                        seen[c, d] = True
                        q.append((c, d))
            if len(comp) >= min_size:
                for a, b in comp:
                    out[a, b] = True
    return out if out.any() else None
