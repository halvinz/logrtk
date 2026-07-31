"""
geoexport.py — Superposition de la carte du robot sur une vue aérienne.

Les logs RTK2 contiennent l'ancrage GPS de la carte (map/pose_graph/
anchor_rtk_info.txt) : point d'origine et rotation par rapport au nord.
On peut donc convertir les positions du robot, exprimées en mètres dans
son repère, en latitude / longitude, et produire un fichier KML.

Ce KML s'ouvre dans Google Earth ou s'importe dans Google My Maps, qui
l'affichent par-dessus la photo satellite : c'est la superposition
demandée, sans dépendre d'une clé d'API ni d'une connexion depuis
l'application.
"""

from __future__ import annotations

import math

# Rayon terrestre moyen : à l'échelle d'un jardin, l'approximation plane
# est très largement suffisante (erreur bien inférieure au centimètre).
METERS_PER_DEGREE = 111320.0


class Georef:
    """Conversion mètres (repère du robot) → latitude / longitude."""

    def __init__(self, lat: float, lon: float, rotation_deg: float = 0.0):
        self.lat = lat
        self.lon = lon
        self.rotation = math.radians(rotation_deg)
        self._cos_lat = math.cos(math.radians(lat)) or 1e-9

    def to_latlon(self, x: float, y: float) -> tuple:
        """(x, y) en mètres dans le repère de la carte → (latitude, longitude)."""
        c, s = math.cos(self.rotation), math.sin(self.rotation)
        east = x * c - y * s
        north = x * s + y * c
        return (self.lat + north / METERS_PER_DEGREE,
                self.lon + east / (METERS_PER_DEGREE * self._cos_lat))


def _placemark(name: str, color: str, coords: list, width: int = 3) -> str:
    """`color` au format KML aabbggrr (alpha, bleu, vert, rouge)."""
    line = " ".join(f"{lon:.8f},{lat:.8f},0" for lat, lon in coords)
    return f"""  <Placemark>
    <name>{name}</name>
    <Style><LineStyle><color>{color}</color><width>{width}</width></LineStyle></Style>
    <LineString><tessellate>1</tessellate><coordinates>{line}</coordinates></LineString>
  </Placemark>
"""


def _point(name: str, lat: float, lon: float) -> str:
    return f"""  <Placemark>
    <name>{name}</name>
    <Point><coordinates>{lon:.8f},{lat:.8f},0</coordinates></Point>
  </Placemark>
"""


# Couleurs KML (aabbggrr) accordées à celles de la carte du logiciel
ZONE_KML_COLORS = ["ff4f9e1f", "ff3da3e8", "ffe87f3d", "ffb6599b",
                   "ffa3a316", "ff7a58d4"]


def mask_to_polygons(mask, resolution, x0=0, y0=0, max_vertices=400) -> list:
    """Contour d'un masque de carte, en mètres.

    Le tracé des contours est confié à matplotlib, déjà utilisé pour la
    carte : inutile d'écrire un suivi de frontière à la main.
    """
    try:
        import numpy as np
        from matplotlib.figure import Figure

        arr = np.asarray(mask, dtype=float)
        if arr.ndim != 2 or arr.max() <= 0:
            return []
        fig = Figure()
        ax = fig.add_subplot(111)
        cs = ax.contour(arr, levels=[0.5])
        # get_paths() sur les versions récentes, allsegs sur les anciennes
        try:
            segments = [poly for path in cs.get_paths()
                        for poly in path.to_polygons()]
        except AttributeError:
            segments = cs.allsegs[0]
        fig.clf()
    except Exception:
        return []

    polygons = []
    for seg in segments:
        if len(seg) < 8:
            continue                      # bruit isolé
        step = max(1, len(seg) // max_vertices)
        pts = [((x0 + x) * resolution, (y0 + y) * resolution)
               for x, y in seg[::step]]
        if pts[0] != pts[-1]:
            pts.append(pts[0])            # un polygone KML doit être fermé
        polygons.append(pts)
    return polygons


def _polygon(name: str, fill: str, line: str, coords: list) -> str:
    ring = " ".join(f"{lon:.8f},{lat:.8f},0" for lat, lon in coords)
    return f"""  <Placemark>
    <name>{name}</name>
    <Style>
      <LineStyle><color>{line}</color><width>2</width></LineStyle>
      <PolyStyle><color>{fill}</color></PolyStyle>
    </Style>
    <Polygon><tessellate>1</tessellate><outerBoundaryIs><LinearRing>
      <coordinates>{ring}</coordinates>
    </LinearRing></outerBoundaryIs></Polygon>
  </Placemark>
"""


def plm_placemarks(plm_map, georef: Georef) -> str:
    """Contour du terrain et îlots issus de la carte .plm du robot."""
    if plm_map is None or georef is None:
        return ""
    out = []
    for layer in plm_map.layers:
        if layer.kind == 2:
            name, fill, line = "Terrain", "4d50b478", "ff2e7d32"
        elif layer.kind == 3:
            name, fill, line = f"Îlot {layer.id}", "b3404040", "ff202020"
        else:
            continue
        arr = layer.to_array()
        if arr is None:
            continue
        for poly in mask_to_polygons(arr, plm_map.resolution,
                                     layer.x0, layer.y0):
            out.append(_polygon(name, fill, line,
                                [georef.to_latlon(x, y) for x, y in poly]))
    return "".join(out)


def build_kml(points, zones, station=None, georef: Georef = None,
              title: str = "Tonte du robot", max_points: int = 6000,
              plm_map=None) -> str:
    """Construit le KML : un tracé par zone de tonte, plus la station.

    `points` : liste de TrackPoint, `zones` : la zone de chacun.
    """
    if georef is None or (not points and plm_map is None):
        return ""
    points = points or []
    zones = zones or []

    # Une trace d'un jour peut compter des dizaines de milliers de points :
    # on l'allège pour que Google Earth reste fluide.
    step = max(1, len(points) // max_points)
    # le terrain d'abord : il sert de fond aux tracés posés par-dessus
    body = [plm_placemarks(plm_map, georef)]
    by_zone = {}
    for p, z in zip(points[::step], zones[::step]):
        by_zone.setdefault(z, []).append(georef.to_latlon(p.x, p.y))

    for zone in sorted(by_zone):
        coords = by_zone[zone]
        if len(coords) < 2:
            continue
        if zone == 0:
            name, color = "Liaison entre zones", "ffb4b4b4"
        else:
            name = f"Zone {zone}"
            color = ZONE_KML_COLORS[(zone - 1) % len(ZONE_KML_COLORS)]
        body.append(_placemark(name, color, coords))

    if station is not None:
        lat, lon = georef.to_latlon(station[0], station[1])
        body.append(_point("Station de charge", lat, lon))

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
<Document>
  <name>{title}</name>
{''.join(body)}</Document>
</kml>
"""


def maps_url(georef: Georef, points=None) -> str:
    """Lien Google Maps centré sur le terrain, en vue satellite."""
    lat, lon = georef.lat, georef.lon
    if points:
        mid = points[len(points) // 2]
        lat, lon = georef.to_latlon(mid.x, mid.y)
    return f"https://www.google.com/maps/@{lat:.7f},{lon:.7f},80m/data=!3m1!1e3"
