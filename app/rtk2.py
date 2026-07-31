"""
rtk2.py — Lecture des exports de logs de la génération RTK2 (KR281E…).

Rien à voir avec l'ancien format : au lieu de deux ou trois gros fichiers,
l'export est une arborescence

    log/    planner.log, mower.log, hmi.log, iot.log, ucm.log, …
    map/    trajectoires, pose_graph/trajectory_state.yaml, cartes .plm
    slam/   calibration

Deux différences utiles :

1. Chaque ligne porte déjà sa gravité et son module :
       2026-07-21 10:52:43.422 [2642] W/MAPCOM    : Failed to read plm file
   On peut donc établir un diagnostic sans disposer d'une « Log bible »
   pour ce modèle : le robot dit lui-même ce qui est un avertissement.

2. La position du robot est loguée par le planificateur :
       planner pose: 4.966464 19.389036 -147.757919      (x, y, cap°)
   et la station de charge est donnée explicitement dans
   map/pose_graph/trajectory_state.yaml (base_position).
"""

from __future__ import annotations

import os
import re
import glob
from datetime import datetime


TS_FMT = "%Y-%m-%d %H:%M:%S.%f"

LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\s+\[(\d+)\]\s+"
    r"([IWEDF])/\s*(\S*)\s*:\s?(.*)$"
)
POSE_RE = re.compile(
    r"planner pose:\s*(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s+(-?\d+\.?\d*)"
)

LEVELS = {"I": "INFO", "W": "WARN", "E": "ERROR", "D": "DEBUG", "F": "ERROR"}

# Journaux d'événements réellement exploitables. Les autres sont soit du
# bruit système (tables CPU, dmesg), soit de la télémétrie brute de plusieurs
# dizaines de Mo qui ferait ramer le chargement sans rien apprendre.
EVENT_LOGS = {
    "planner", "mower", "hmi", "iot", "ucm", "ssm", "em", "percep",
    "percepfront", "slam", "slam_important", "sensor", "watch", "ui",
    "fct", "pns_srv", "body", "mcu", "comm",
}
MAX_FILE_BYTES = 40 * 1024 * 1024


def is_useful_member(name: str) -> bool:
    """Fichiers d'une archive qu'il vaut la peine d'extraire. Les exports
    pèsent 450 Mo dont l'essentiel — télémétrie SLAM brute, dmesg, captures
    PNG — n'est jamais lu : les sortir tous ralentirait l'ouverture pour rien.
    """
    name = name.replace("\\", "/")
    if name.endswith("/"):
        return False
    base = name.rsplit("/", 1)[-1]
    if name.endswith(".log"):
        stem = base[:-4]
        if stem.endswith(".1"):
            stem = stem[:-2]
        return stem in EVENT_LOGS
    # descripteurs de carte : station de charge, résolution, progression
    return base.endswith((".yaml", ".json")) and (
        name.startswith("map/") or "/map/" in name
    )


def archive_is_rtk2(names) -> bool:
    """Vrai si l'archive contient les journaux caractéristiques du RTK2."""
    bases = {n.replace("\\", "/").rsplit("/", 1)[-1] for n in names}
    return bool({"planner.log", "mower.log"} & bases)


def looks_like_rtk2(folder: str) -> bool:
    """Vrai si le dossier ressemble à un export RTK2."""
    return bool(_log_dir(folder))


def _log_dir(folder: str) -> str | None:
    for candidate in (os.path.join(folder, "log"), folder):
        if not os.path.isdir(candidate):
            continue
        names = {os.path.basename(p) for p in glob.glob(os.path.join(candidate, "*.log"))}
        if {"planner.log", "mower.log"} & names:
            return candidate
    return None


def _event_files(log_dir: str) -> list:
    """Fichiers à charger, rotations .1.log comprises (historique plus long)."""
    out = []
    for path in sorted(glob.glob(os.path.join(log_dir, "*.log"))):
        base = os.path.basename(path)
        stem = base[:-4]                      # sans .log
        if stem.endswith(".1"):
            stem = stem[:-2]
        if stem not in EVENT_LOGS:
            continue
        try:
            if os.path.getsize(path) > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        out.append(path)
    return out


def _parse_ts(s: str):
    try:
        return datetime.strptime(s, TS_FMT)
    except ValueError:
        return None


def parse_file(path: str, session) -> None:
    """Lit un journal RTK2 : lignes horodatées + positions du planificateur."""
    from parser import LogLine, TrackPoint     # import tardif : évite un cycle

    fname = os.path.basename(path)
    with open(path, "r", errors="ignore") as f:
        for raw in f:
            raw = raw.rstrip("\r\n")
            if not raw:
                continue
            m = LINE_RE.match(raw)
            if not m:
                # continuation d'une ligne précédente (pile d'appel, tableau…)
                session.lines.append(LogLine(None, "-", raw, raw, fname))
                continue
            ts_s, _tid, lvl, tag, msg = m.groups()
            ts = _parse_ts(ts_s)
            session.lines.append(
                LogLine(ts, LEVELS.get(lvl, "INFO"), msg, raw, fname, tag)
            )
            if ts is not None:
                pm = POSE_RE.search(msg)
                if pm:
                    session.track.append(
                        TrackPoint(ts, float(pm.group(1)), float(pm.group(2)),
                                   float(pm.group(3)), 0)
                    )
    session.files_loaded.append(fname)


def read_station(folder: str):
    """Position de la station de charge, lue dans le graphe de poses SLAM
    (base_position). Contrairement au format RTK1, elle est donnée telle
    quelle : inutile de la deviner à partir des passages en charge."""
    path = os.path.join(folder, "map", "pose_graph", "trajectory_state.yaml")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", errors="ignore") as f:
            text = f.read()
    except OSError:
        return None
    m = re.search(
        r"base_position:\s*\n\s*position:\s*\n\s*x:\s*(-?[\d.eE+]+)\s*\n\s*y:\s*(-?[\d.eE+]+)",
        text,
    )
    if not m:
        return None
    try:
        return float(m.group(1)), float(m.group(2))
    except ValueError:
        return None


def read_identity(folder: str, session) -> None:
    """Modèle et numéro de série : l'export ne contient pas d'en-tête comme
    en RTK1, mais le nom du dossier suit le motif MODELE_SERIE_date."""
    name = os.path.basename(os.path.normpath(folder))
    m = re.match(r"([A-Z]{2}\d{3}[A-Z]*)_([0-9A-Za-z]+)_", name)
    if m:
        session.model = session.model or m.group(1)
        session.serial = session.serial or m.group(2)
    m = re.search(r"_(\d{4})-(\d{1,2})-(\d{1,2})_(\d{1,2})_(\d{1,2})", name)
    if m:
        session.export_date = session.export_date or (
            f"{m.group(3).zfill(2)}/{m.group(2).zfill(2)}/{m.group(1)} "
            f"{m.group(4).zfill(2)}:{m.group(5).zfill(2)}"
        )


def load(folder: str, session) -> None:
    """Charge un export RTK2 complet dans `session`."""
    log_dir = _log_dir(folder)
    if not log_dir:
        return
    session.fmt = "rtk2"
    # racine de l'export : le dossier contenant log/, map/, slam/
    root = os.path.dirname(log_dir) if os.path.basename(log_dir) == "log" else folder
    for path in _event_files(log_dir):
        parse_file(path, session)
    session.station_xy = read_station(root)
    read_identity(root, session)
