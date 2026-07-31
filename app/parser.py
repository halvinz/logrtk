"""
parser.py
Analyse les fichiers de logs exportés par le robot (tondeuse Positec/Kress
et compatibles) : fichier principal ..._MODEL.log, ..._pos.log, ..._boot.log,
dmesg.txt.

Le tracé du robot est reconstruit à partir des positions SLAM loguées.
La carte .plm, elle, est lue par le module `plm` : contrairement à ce qu'on
croyait, elle n'est pas chiffrée mais simplement compressée.
"""

from __future__ import annotations

import os
import re
import glob
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


TS_RE = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})"

# Deux variantes rencontrées :
#   2026-07-24 14:34:12.142:[INFO]  Slam(17.454 10.966 -124.880 2) IMU(0.138 0.349 -3.387)
#   2026-07-27 01:40:27.006:[INFO]  Slam 37.597 29.705 90.000 166 IMU 55.716 0.278 1.789
SLAM_RE = re.compile(
    TS_RE + r":\[(\w+)\]\s+Slam\(?\s*"
    r"(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s+(-?\d+)\)?"
)

LOG_LINE_RE = re.compile(TS_RE + r":\[(\w+)\]\s*(.*)")

TS_FMT = "%Y-%m-%d %H:%M:%S.%f"


@dataclass
class TrackPoint:
    ts: datetime
    x: float
    y: float
    heading: float
    mode: int


@dataclass
class LogLine:
    ts: Optional[datetime]
    level: str
    text: str
    raw: str
    source: str  # nom du fichier d'origine
    tag: str = ""  # module émetteur (logs RTK2 : TASK, MAPPING, HMI…)


@dataclass
class RobotSession:
    model: str = ""
    serial: str = ""
    firmware: str = ""
    export_date: str = ""
    battery_recharged: str = ""
    blade_running_time: str = ""
    total_work_time: str = ""
    total_distance: str = ""
    boundary_length: str = ""
    schedule: list = field(default_factory=list)
    track: list = field(default_factory=list)      # list[TrackPoint]
    lines: list = field(default_factory=list)       # list[LogLine]
    files_loaded: list = field(default_factory=list)
    fmt: str = "rtk1"        # "rtk1" (ancien format) ou "rtk2"
    station_xy: tuple = None       # station de charge, si les logs la donnent
    station_heading: float = None  # cap de l'axe d'accostage, en degrés
    robot_map: tuple = None        # (masque de la carte du robot, résolution m)
    geo_anchor: tuple = None       # (latitude, longitude, rotation°) de l'origine


def _parse_timestamp(s: str) -> Optional[datetime]:
    try:
        return datetime.strptime(s, TS_FMT)
    except ValueError:
        return None


def parse_main_log_header(path: str, session: RobotSession) -> None:
    """Extrait le bloc d'en-tête + statistiques du fichier ..._MODEL.log."""
    with open(path, "r", errors="ignore") as f:
        head = f.read(4000)

    def grab(pattern, default=""):
        m = re.search(pattern, head)
        return m.group(1).strip() if m else default

    session.export_date = grab(r"Data:\s*(.+)")
    session.model = grab(r"Model:\s*(.+)")
    session.serial = grab(r"Serial Number:\s*(.+)")
    session.firmware = grab(r"Firmware Version:\s*(.+)")
    session.battery_recharged = grab(r"Battery recharged:\s*(.+)")
    session.blade_running_time = grab(r"Blade running time:\s*(.+)")
    session.total_work_time = grab(r"Total work time:\s*(.+)")
    session.total_distance = grab(r"Total distance\s*(.+)")
    session.boundary_length = grab(r"Boundary length:\s*(.+)")

    sched_match = re.search(r"SCHEDULER(.*?)(?:\n-{3,}|\Z)", head, re.S)
    if sched_match:
        for line in sched_match.group(1).splitlines():
            line = line.strip().strip("\r")
            if ":" in line and "Worktime" not in line and line:
                session.schedule.append(line)


def parse_log_file_for_track_and_lines(path: str, session: RobotSession) -> None:
    """Parcourt un fichier log ligne par ligne : extrait les points Slam et
    conserve toutes les lignes (pour la visionneuse de logs)."""
    fname = os.path.basename(path)
    with open(path, "r", errors="ignore") as f:
        for raw in f:
            raw = raw.rstrip("\r\n")
            if not raw:
                continue

            m = SLAM_RE.search(raw)
            if m:
                ts_s, level, x, y, heading, mode = m.groups()
                ts = _parse_timestamp(ts_s)
                if ts is not None:
                    session.track.append(
                        TrackPoint(ts, float(x), float(y), float(heading), int(mode))
                    )

            lm = LOG_LINE_RE.match(raw)
            if lm:
                ts_s, level, text = lm.groups()
                ts = _parse_timestamp(ts_s)
                session.lines.append(LogLine(ts, level, text, raw, fname))
            else:
                # ligne système / dmesg / sans timestamp reconnu
                session.lines.append(LogLine(None, "-", raw, raw, fname))

    session.files_loaded.append(fname)


def autodetect_files(folder: str) -> dict:
    """Devine les fichiers pertinents dans un dossier exporté par le robot,
    à partir des motifs de nommage observés :
        <date>_<MODEL>.log
        <date>_<MODEL>_pos.log
        <date>_<MODEL>_path.log
        <date>_<MODEL>_boot.log
        dmesg.txt
    """
    result = {"main": None, "pos": None, "path": None, "boot": None, "dmesg": None}

    boot_candidates = glob.glob(os.path.join(folder, "*_boot.log"))
    pos_candidates = glob.glob(os.path.join(folder, "*_pos.log"))
    path_candidates = glob.glob(os.path.join(folder, "*_path.log"))
    dmesg_candidates = glob.glob(os.path.join(folder, "dmesg*.txt"))

    all_logs = set(glob.glob(os.path.join(folder, "*.log")))
    excluded = set(boot_candidates) | set(pos_candidates) | set(path_candidates)
    main_candidates = sorted(all_logs - excluded)

    if main_candidates:
        result["main"] = main_candidates[0]
    if pos_candidates:
        result["pos"] = pos_candidates[0]
    if path_candidates:
        result["path"] = path_candidates[0]
    if boot_candidates:
        result["boot"] = boot_candidates[0]
    if dmesg_candidates:
        result["dmesg"] = dmesg_candidates[0]

    return result


def load_session(main=None, pos=None, path=None, boot=None, dmesg=None) -> RobotSession:
    session = RobotSession()

    if main and os.path.isfile(main):
        parse_main_log_header(main, session)
        parse_log_file_for_track_and_lines(main, session)

    for extra in (pos, path, boot, dmesg):
        if extra and os.path.isfile(extra):
            parse_log_file_for_track_and_lines(extra, session)

    session.track.sort(key=lambda p: p.ts)
    session.lines.sort(key=lambda l: (l.ts is None, l.ts))
    return session


def load_session_from_folder(folder: str) -> RobotSession:
    """Charge un dossier de logs, quel que soit le format du robot."""
    import rtk2

    if rtk2.looks_like_rtk2(folder):
        session = RobotSession()
        rtk2.load(folder, session)
        session.track.sort(key=lambda p: p.ts)
        session.lines.sort(key=lambda l: (l.ts is None, l.ts))
        return session

    files = autodetect_files(folder)
    return load_session(**files)


def load_session_from_archive(path: str) -> RobotSession:
    """Charge une archive d'export sans décompression manuelle.

    Deux formats selon la génération : le robot RTK1 produit un .tar.gz,
    le RTK2 un .zip. Ce dernier fait échouer l'Explorateur Windows à cause
    de la longueur des chemins, autant s'en charger nous-mêmes.
    """
    import tarfile

    if tarfile.is_tarfile(path):
        return _load_from_tar(path)
    return _load_from_zip(path)


def _extract_dir(path: str) -> str:
    import tempfile

    name = os.path.basename(path)
    for suffix in (".tar.gz", ".tgz", ".tar", ".zip"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    target = os.path.join(tempfile.gettempdir(), "RobotLogViewer_extract", name)
    os.makedirs(target, exist_ok=True)
    return target


def _load_from_tar(tar_path: str) -> RobotSession:
    import tarfile

    target = _extract_dir(tar_path)
    with tarfile.open(tar_path) as t:
        for member in t.getmembers():
            if not member.isfile():
                continue
            if not member.name.endswith((".log", ".txt")):
                continue
            src = t.extractfile(member)
            if src is None:
                continue
            dest = os.path.join(target, os.path.basename(member.name))
            with src, open(dest, "wb") as out:
                out.write(src.read())
    return load_session_from_folder(target)


def _load_from_zip(zip_path: str) -> RobotSession:
    import zipfile
    import rtk2

    target = _extract_dir(zip_path)

    with zipfile.ZipFile(zip_path) as z:
        # une archive RTK1 ne contient que quelques journaux : on prend tout ;
        # une archive RTK2 en contient 450 Mo dont on n'utilise qu'une part
        rtk2_archive = rtk2.archive_is_rtk2(z.namelist())
        for info in z.infolist():
            if info.is_dir():
                continue
            name = info.filename
            if rtk2_archive:
                if not rtk2.is_useful_member(name):
                    continue
            elif not name.endswith((".log", ".txt")):
                continue
            dest = os.path.join(target, *name.split("/"))
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with z.open(info) as src, open(dest, "wb") as out:
                    out.write(src.read())
            except OSError:
                continue  # nom de fichier impossible sous Windows : on l'ignore

    return load_session_from_folder(target)
