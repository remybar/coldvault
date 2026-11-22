# Lancement et distribution

Le moteur se lance avec `uv run coldvault ...`, l'interface avec
`uv run --extra gui coldvault-gui`. Les commandes et tests sont destinés à macOS,
Windows et Linux ; les workflows sont fournis pour exécuter le même code sur chaque OS.

L'extra `gui` utilise PySide6-Essentials, sans les addons ni WebEngine. Les opérations
disque utilisent un worker QThread, sans dupliquer la logique du moteur.
PyInstaller se construit nativement sur chaque OS (pas de compilation croisée) :

```sh
uv sync --locked --extra gui --group build --group dev
uv run --extra gui --group build pytest
uv run --extra gui --group build pyinstaller --noconfirm ColdVault.spec
```

`ColdVault.spec` produit un dossier autonome `dist/ColdVault` et, sur macOS, un bundle
`dist/ColdVault.app`. Distribuer le dossier entier, pas seulement le binaire. La
spécification du manifeste et le vérificateur autonome sont joints aux ressources.
Les dépendances Qt ont leurs propres licences et notices, indépendantes de la licence
MIT du code ColdVault ; les conserver lors de toute redistribution.

Un contrôle de démarrage du bundle ouvre puis ferme l'accueil, sans charger le profil
ni accéder à un disque utilisateur : `ColdVault --smoke-test` (adapter le chemin au binaire).
Avec `QT_QPA_PLATFORM=offscreen`, ce contrôle fonctionne sans affichage interactif.

- macOS : ouvrir `.app` depuis le Finder, construit séparément pour les architectures retenues ; signature
  Developer ID et notarisation Apple pour une distribution publique plus fluide.
- Windows : double-cliquer `dist/ColdVault/ColdVault.exe`, signature Authenticode facultative ; SmartScreen
  peut avertir pour un binaire non signé ou sans réputation, y compris après signature.
- Linux : lancer `dist/ColdVault/ColdVault` ou installer `packaging/ColdVault.desktop`
  dans `~/.local/share/applications` après avoir remplacé `Exec` par le chemin absolu du
  binaire. Le workflow construit sur Ubuntu 22.04 (glibc 2.35) ; le minimum réel est
  aussi contraint par les versions de Python/Qt embarquées et doit être testé.

Une application macOS non signée peut déclencher Gatekeeper. Le packaging ne supprime pas
les contrôles de l'OS ; ne pas demander de désactiver globalement ses protections.
Les versions signées nécessitent des certificats et secrets gérés hors du dépôt.
Le fonctionnement de l'application et la lecture des archives restent hors ligne.

Le workflow manuel `.github/workflows/build.yml` teste puis construit sur les trois OS,
et joint des artefacts temporaires non signés. Il ne crée pas de release publique.
Le bundle local macOS ARM64 a été construit ; ce n'est pas une preuve de compatibilité
avec tous les Macs ni de passage de Gatekeeper. Les autres plateformes restent à valider.

Références : [PyInstaller](https://pyinstaller.org/en/stable/usage.html),
[threads Qt](https://doc.qt.io/qtforpython-6/PySide6/QtCore/QThread.html).

Avant publication : lancer la suite sur les trois OS, tester les builds sur machine propre,
tester annulation/débranchement sur un support **jetable** dédié, vérifier le comportement
exFAT et joindre la spécification du manifeste et le lecteur de secours aux paquets.
