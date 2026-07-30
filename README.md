# Robot Log Viewer

Visionneuse de logs pour robot (testé avec des exports de tondeuse Positec/Kress,
modèle KR172EA, mais fonctionne avec tout robot qui produit des logs au même
format `AAAA-MM-JJ HH:MM:SS.mmm:[NIVEAU] message`).

## Ce que ça fait

- Ouvre un dossier de logs exporté par le robot (le `.tar` que le robot génère)
  et détecte automatiquement les fichiers `..._MODEL.log`, `..._pos.log`,
  `..._boot.log`, `dmesg.txt`.
- Affiche les informations du robot (modèle, série, firmware, statistiques
  d'utilisation, planning).
- Reconstruit et affiche le trajet du robot à partir des positions SLAM
  loguées (dégradé bleu → rouge dans le temps, comme l'outil d'origine).
- Filtre par plage de dates.
- Visionneuse de logs avec recherche texte, filtre par niveau (INFO/WARN/ERROR)
  et par fichier source.

## Limite connue

Les fichiers `.plm` (carte / zones tondues) utilisés par l'outil d'origine
sont dans un format binaire propriétaire non documenté et ne sont pas
décodés ici — le fond de carte (zone verte tondue) n'est donc pas reproduit.
Le trajet du robot (le tracé bleu/rouge, la donnée la plus utile pour le
diagnostic) est lui entièrement reconstruit à partir des logs texte.

## Utilisation en développement (sans compiler)

```
pip install -r requirements.txt
python app/main.py
```

## Générer le .exe Windows

Sur une machine Windows avec Python 3.10+ installé (python.org, cocher
"Add python.exe to PATH" à l'installation) :

1. Copier tout le dossier `RobotLogViewer` sur la machine Windows.
2. Double-cliquer sur `build_exe.bat`.
3. Le fichier `RobotLogViewer.exe` apparaît dans `dist\`. Il est autonome,
   pas besoin de réinstaller Python pour l'utiliser ensuite.

## Structure du projet

```
RobotLogViewer/
├── app/
│   ├── main.py      point d'entrée
│   ├── gui.py        interface (PySide6 + matplotlib)
│   └── parser.py     analyse des fichiers de logs
├── requirements.txt
├── build_exe.bat      script de build Windows en un clic
└── README.md
```
