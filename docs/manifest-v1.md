# Manifeste public ColdVault v1

Document JSON UTF-8 sans BOM, fins de ligne LF. Les champs inconnus, clés dupliquées,
totaux incohérents, empreintes invalides et chemins dangereux sont refusés par le lecteur.
Le versionnement du format est indépendant de celui de l'application.
Une évolution incompatible changera `format_version` ; aucun upgrade silencieux.

```json
{
  "format_version": 1,
  "archive_id": "12345678-1234-4234-8234-123456789abc",
  "name": "Famille",
  "generation": 1,
  "created_utc": "2026-11-22T12:00:00Z",
  "roots": ["Photos"],
  "files": [
    {
      "path": "data/000001/Photos/vide.txt",
      "logical_path": "Photos/vide.txt",
      "size": 0,
      "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "revision": 1,
      "mtime_ns": null
    }
  ],
  "total_files": 1,
  "total_bytes": 0,
  "parent_sha256": null
}
```

| Champ | Signification |
| --- | --- |
| `format_version` | Entier 1, jamais un booléen |
| `archive_id` | UUID canonique minuscule, stable sur toutes les générations/supports |
| `name` | Nom utilisateur non vide |
| `generation` | Entier de 1 à 999999, croissant de 1 |
| `created_utc` | Date ISO 8601 en UTC terminant par `Z` |
| `roots` | Tableau non vide d'alias simples, uniques, sensibles à leur orthographe |
| `files` | Toutes les versions conservées, triées par `path` lors de la préparation |
| `total_files` | Nombre d'entrées, versions historiques comprises |
| `total_bytes` | Somme des tailles de toutes les entrées |
| `parent_sha256` | `null` en génération 1 ; SHA-256 canonique du parent ensuite |

Chaque entrée contient un `path` physique relatif au dossier d'archive, un `logical_path`
relatif à l'ensemble des racines, la taille entière ≥ 0, le SHA-256 en 64 chiffres hex
minuscules, la `revision` (génération d'ajout) et `mtime_ns` (entier indicatif ou `null`).
La date de modification ne justifie jamais une validation ni une exclusion de vérification.

`path` doit être exactement `data/{revision sur 6 chiffres}/{logical_path}`.
`logical_path` commence par un alias connu, suivi d'au moins un composant fichier.
Les séparateurs sont toujours `/`. Aucun chemin absolu, lecteur, UNC, `..`, composant
vide ou `.` n'est autorisé. Backslash, caractères de contrôle et noms Windows réservés
sont refusés ; les noms terminant par un point/espace également.
Les noms de composants sont limités à 255 octets UTF-8 et 255 unités UTF-16 ; les chemins
relatifs à 220 unités UTF-16. Les répertoires parents participent aux contrôles de casse
et de normalisation NFC. La limite Windows absolue est contrôlée lors des E/S.

Les versions d'un même fichier partagent `logical_path`, mais ont des `revision` et
donc `path` différents. Prendre la plus grande révision pour retrouver la dernière
version connue. L'absence d'un fichier des sources ne retire aucune entrée.
Le manifeste conserve l'histoire ; il ne décrit pas un miroir avec suppressions.

## Empreinte du manifeste

L'encodage canonique utilise la sérialisation Python standard :

```python
(json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
```

L'ordre des tableaux est conservé. Il n'y a pas de flottants dans le format.
Le SHA-256 de ces octets est utilisé pour la filiation et le nom
`gNNNNNN-SHA256.json`. L'empreinte ne figure pas dans le manifeste lui-même.
L'empreinte des fichiers concerne uniquement leurs octets, sans métadonnées ou encodage.

## Disposition sur le support

```text
Famille/
  data/000001/Photos/...
  data/000002/Photos/...
  .coldvault/
    media.json
    operation.lock
    manifests/g000001-<sha256>.json
    manifests/g000002-<sha256>.json
    operations/<uuid>.start.json
    operations/<uuid>.finish.json
    reports/<uuid>.json
    residuals/<uuid>.part
```

`.coldvault` est réservé au contrôle et exclu de la recherche des fichiers supplémentaires.
Ne jamais y placer de contenu utilisateur. Les temporaires de copie `.cv-<uuid>.part`
se trouvent initialement à côté du fichier final. Ils ne sont jamais des fichiers validés.

Les rapports enregistrent identifiants archive/support, empreinte de manifeste, génération,
dates UTC début/fin, contexte (`immediate`, `standalone`, `after_reconnection`), preuve de
reconnexion (`none` ou `user_declaration`), résultats et booléen global `ok`.
Les erreurs de copie ont un rapport distinct `context=copy_failed` lorsque le support
permet encore d'écrire. Les détails d'erreur sont informatifs, pas des chemins à exécuter.

Il n'existe ni chiffrement ni signature d'authenticité dans ce format. Conserver une
copie de confiance du manifeste et des rapports hors du support.
