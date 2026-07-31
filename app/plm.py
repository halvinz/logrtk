"""
plm.py — Lecture des cartes .plm exportées par le robot.

Ce format était réputé indéchiffrable : il n'est en fait pas chiffré, juste
compressé. Sa structure calque celle d'un PNG.

    89 'P' 'L' 'M' 0d 0a 1a 0a      signature, comme PNG mais « PLM »
    version(4) = 1
    longueur d'en-tête(4) = 24
    en-tête : … résolution en mm(4) … largeur(4) hauteur(4) …
    puis une suite de sections :
        empreinte(4) longueur(4) genre(2) sous-genre(2) données
    où « données » est un flux deflate brut (sans en-tête zlib).

Chaque section décompressée contient une couche :

    id(4) largeur(4) hauteur(4) décalage_x(4) décalage_y(4)
    puis 1 bit par pixel, bit de poids fort en premier, ligne par ligne.

Genres observés :
    2  surface de tonte
    3  îlot : massif, arbre, bassin — les taches sombres de l'application
    5  couche pleine supplémentaire (bordure / exclusion)
"""

from __future__ import annotations

import struct
import zlib

MAGIC = b"\x89PLM\r\n\x1a\n"

KIND_AREA = 2
KIND_ISLAND = 3
KIND_EXTRA = 5

KIND_LABELS = {
    KIND_AREA: "Surface de tonte",
    KIND_ISLAND: "Îlot / obstacle",
    KIND_EXTRA: "Couche complémentaire",
}


class Layer:
    def __init__(self, kind, lid, w, h, x0, y0, bits):
        self.kind = kind
        self.id = lid
        self.width = w
        self.height = h
        self.x0 = x0          # décalage en pixels dans la carte complète
        self.y0 = y0
        self.bits = bits      # 1 bit par pixel, poids fort d'abord

    @property
    def label(self):
        return KIND_LABELS.get(self.kind, f"Genre {self.kind}")

    def to_array(self):
        """Masque booléen (hauteur × largeur) — nécessite numpy."""
        import numpy as np

        need = self.width * self.height
        flat = np.unpackbits(np.frombuffer(self.bits, dtype=np.uint8))
        if flat.size < need:
            return None
        return flat[:need].reshape(self.height, self.width).astype(bool)


class PlmMap:
    def __init__(self, resolution, width, height, layers):
        self.resolution = resolution    # mètres par pixel
        self.width = width              # en pixels
        self.height = height
        self.layers = layers

    def layers_of(self, kind):
        return [l for l in self.layers if l.kind == kind]

    def summary(self) -> str:
        counts = {}
        for l in self.layers:
            counts[l.label] = counts.get(l.label, 0) + 1
        detail = ", ".join(f"{n} × {k.lower()}" for k, n in sorted(counts.items()))
        return (f"{self.width}×{self.height} px à {self.resolution:.2f} m "
                f"({self.width * self.resolution:.0f} × "
                f"{self.height * self.resolution:.0f} m) — {detail}")


def read_plm(path: str) -> PlmMap | None:
    """Lit une carte .plm. Renvoie None si le fichier n'est pas de ce format."""
    with open(path, "rb") as f:
        data = f.read()
    if not data.startswith(MAGIC):
        return None

    header_len = struct.unpack(">I", data[12:16])[0]
    head = data[16:16 + header_len]
    if len(head) < 16:
        return None
    resolution_mm, width, height = struct.unpack(">3I", head[4:16])
    resolution = (resolution_mm or 50) / 1000.0

    layers = []
    pos = 16 + header_len + 4      # 4 octets de contrôle après l'en-tête
    while pos + 12 <= len(data):
        # empreinte(4) longueur(4) genre(2) sous-genre(2) puis les données
        length = struct.unpack(">I", data[pos + 4:pos + 8])[0]
        kind = struct.unpack(">H", data[pos + 8:pos + 10])[0]
        if length <= 0 or pos + 12 + length > len(data):
            break
        body = data[pos + 12:pos + 12 + length]
        pos += 12 + length
        if len(body) < 8:
            continue
        try:
            raw = zlib.decompressobj(-15).decompress(body, 8 << 20)
        except zlib.error:
            continue
        if len(raw) < 20:
            continue
        lid, w, h, x0, y0 = struct.unpack(">5I", raw[:20])
        if not (0 < w <= 20000 and 0 < h <= 20000):
            continue
        if (len(raw) - 20) * 8 < w * h:
            continue
        layers.append(Layer(kind, lid, w, h, x0, y0, raw[20:]))

    if not layers:
        return None
    return PlmMap(resolution, width, height, layers)
