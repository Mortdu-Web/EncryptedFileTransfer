# file-transfer — transfert de fichiers chiffrés entre utilisateurs

Chaque utilisateur envoie des fichiers (chiffrés AES-256-GCM sur le disque du serveur), les partage avec d'autres
utilisateurs, et télécharge ceux qu'on a partagés avec lui. Même squelette que `game-hub` (Flask + gunicorn + Docker,
auth via `auth_client`, base PostgreSQL distante).

## Lancer en local (sans authentification)
```bash
pip install -r requirements.txt      # sans auth-client
cp .env.example .env                 # renseigner ENCRYPTION_KEY, DB_* et DEV_USERNAME ; AUTH_ENABLED=false, URL_PREFIX= (vide)
flask --app wsgi run --debug --port 5000
```
Accueil : `http://localhost:5000/` (ou `http://localhost:5000<URL_PREFIX>/` si un préfixe est défini).
Sans auth, tout le monde est l'utilisateur `DEV_USERNAME`, qui doit exister dans la table `users` ; pour tester un
partage, relancer avec un autre `DEV_USERNAME`.

## Production (avec authentification)
```bash
pip install -r requirements-auth.txt   # ajoute auth-client
# .env : AUTH_ENABLED=true + variables AUTH_* + URL_PREFIX=/file-transfer
```

## Structure
```
wsgi.py                  point d'entrée (gunicorn / flask)
app/
  __init__.py            create_app : préfixe d'URL, proxy, vérification de la clé, base de données
  config.py              lecture du .env
  helpers.py             require_auth (auth_client si AUTH_ENABLED) + filtre « taille lisible »
  crypto.py              chiffrement/déchiffrement AES-256-GCM (seul fichier à adapter pour changer d'algorithme)
  db.py                  PostgreSQL : pool de connexions + utilisateurs, fichiers, partages (seul fichier à adapter pour une autre base)
  schema.sql             migration des tables existantes (files.size_bytes, unicité des partages, index), à lancer une fois
  users.py               utilisateur courant : nom d'utilisateur (Keycloak ou DEV_USERNAME) relié à la table users
  fileservice.py         logique métier : upload / download / delete (droits + chiffrement + disque + base)
  routes.py              blueprint « files » : page, /upload, /download/<id>, /share, DELETE /<id>
  templates/             base.html, files.html, partials/ (_my_files, _shared_files, _icons)
  static/                css/site.css (variables), css/files.css, js/files.js
```

## Routes
- `<URL_PREFIX>/` : mes fichiers + fichiers partagés avec moi
- `<URL_PREFIX>/upload` : POST multipart (`file`)
- `<URL_PREFIX>/download/<id>` : téléchargement (propriétaire ou utilisateur avec qui le fichier est partagé)
- `<URL_PREFIX>/share` : POST JSON `{file_id, user_id, add}` (propriétaire uniquement)
- `<URL_PREFIX>/<id>` : DELETE (propriétaire uniquement)

Toutes les routes exigent `@require_auth()`. Pour restreindre à un rôle Keycloak : `@require_auth("mon-role")`
dans `routes.py`.

## Chiffrement
- Clé : `ENCRYPTION_KEY` (obligatoire, 32 octets en base64, commande de génération dans `.env.example`).
  L'application refuse de démarrer sans clé valide.
- **Perdre ou changer la clé rend tous les fichiers existants illisibles.** La sauvegarder séparément du serveur.
- Le fichier est chiffré/déchiffré **en mémoire** : `MAX_UPLOAD_MB` (100 par défaut) limite la taille, et la RAM
  nécessaire est de l'ordre de la taille du fichier × nombre de workers actifs.
- Sur le disque, les fichiers sont nommés `<uuid>.enc` (sans l'extension d'origine). Le nom d'origine n'est qu'en base.

## Données
Le service utilise les tables du hub, dans la base PostgreSQL distante : `users` (lecture seule : ce service ne crée
ni ne modifie aucun compte), `files` et `file_shares`. Les identifiants sont les `users.id` (entiers).

**Migration, une seule fois**, avec le propriétaire des tables (l'utilisateur de l'application n'a pas besoin de ce droit) :
```bash
sudo -u postgres psql -d <base> -f schema.sql
```
Elle ajoute `files.size_bytes`, une contrainte d'unicité sur `file_shares (file_id, shared_with_user_id)` et deux index ;
elle peut être relancée sans risque. L'utilisateur de l'application doit avoir : `SELECT` sur `users`,
`SELECT/INSERT/UPDATE/DELETE` sur `files` et `file_shares`, et `USAGE, SELECT` sur les séquences `files_id_seq` et
`file_shares_id_seq`.

L'authentification (Keycloak) fournit un nom d'utilisateur, relié à `users.username` : **un compte absent de la table
`users` reçoit une erreur 403.** La liste « Partager avec » est la table `users`.

Au démarrage, l'application vérifie la connexion, les tables et la migration : en cas de problème elle s'arrête avec un
message explicite, au lieu de renvoyer une erreur 500 à la première requête. Les connexions viennent d'un pool
(`DB_POOL_MIN` / `DB_POOL_MAX`, par worker gunicorn) ; ne pas lancer gunicorn avec `--preload`. Base coupée en cours de
route : les requêtes reçoivent un 503, puis tout repart seul au retour de la base.

## Docker
```bash
cp .env.example .env                 # renseigner PORT, SECRET_KEY, ENCRYPTION_KEY et DB_* (migration faite avant)
docker network create web            # une seule fois (réseau partagé avec le reverse proxy / l'auth)
docker compose up -d --build
```
En production (auth activée) : `WITH_AUTH=true docker compose up -d --build` avec `AUTH_ENABLED=true` dans le `.env`.

Les fichiers chiffrés sont dans le volume Docker `uploads` (monté sur `/data/uploads`). **Le sauvegarder, avec la clé.**

### Reverse proxy
Le reverse proxy doit laisser passer les gros fichiers, sinon l'upload échoue avant d'atteindre l'application.
Exemple nginx, dans le bloc `location /file-transfer/` :
```nginx
client_max_body_size 110m;      # un peu plus que MAX_UPLOAD_MB
proxy_read_timeout   120s;
```
Derrière un proxy HTTPS, mettre `BEHIND_PROXY=true` et `SESSION_COOKIE_SECURE=true`.
