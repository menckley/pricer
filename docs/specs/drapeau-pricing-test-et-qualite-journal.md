# Drapeau « test » et statut commercial des pricings

## Objectif
Pouvoir distinguer les pricings d'essai des pricings réels, rattacher un client et suivre l'issue commerciale
(envoyé / gagné / perdu / expiré, prix gagné) afin de calculer plus tard un taux de transformation, d'exclure le
bruit des statistiques et d'alimenter le backtest.

## Périmètre
Inclus : colonnes `client`, `is_test`, `status`, `won_price` (schéma v2 du journal) ; migration versionnée v1 → v2 ;
journal d'événements append-only `pricings/statuts.csv` ; fonction `update_status` (un commit par événement).
Exclus (suites) : onglet de l'UI de mise à jour (`ui.py` n'a pas de client GitHub, l'application Streamlit n'est pas
dans ce dépôt), backtest, analyse « marge vs probabilité de gain », requalification des pricings existants.

## Conception
- `COLUMNS` (schéma v1) est inchangé ; `COLUMNS_V2 = COLUMNS + [client, is_test, status, won_price]`.
- Optionnel : `pricing_files(..., client=None, is_test=None)`. Si aucun n'est fourni, la ligne reste au schéma v1 et le
  comportement actuel est strictement conservé. Sinon la ligne est v2 avec `status="envoyé"`, `won_price=""`.
- `_journal_with` accepte un journal existant en v1 ou v2 (toute autre en-tête est refusée). Si une ligne v2 est ajoutée
  à un journal v1, le journal est migré : en-tête v2, anciennes lignes complétées par des cellules vides (un seul
  commit, rien n'est perdu). Une ligne v1 ajoutée à un journal v2 est complétée par des cellules vides.
- Le statut courant n'est pas réécrit dans `journal.csv` : `pricings/statuts.csv`
  (`event_at_utc,id,status,won_price,note`) est append-only, le dernier événement d'un `id` fait foi
  (`latest_status`). `update_status` vérifie que l'`id` existe dans le journal, que le statut est valide et que
  `won_price` (fini, > 0) n'est renseigné que pour « gagné ».
- Les cellules texte passent par `_csv_safe` (injection de formule).

## Plan
1. Spécification. 2. `journal.py` : schéma v2, migration, événements, `update_status` (réutilise la boucle de
retentative de `push`). 3. Tests.

## Tests
Ligne v1 inchangée par défaut ; ligne v2 ; migration v1 → v2 (anciennes lignes conservées) ; en-tête inconnue refusée ;
événements (id inconnu, statut invalide, prix gagné incohérent, dernier événement gagnant) ; `update_status` bout en
bout sur le faux GitHub (un commit, journal intact). Les tests de cohérence du journal archivé existants restent
inchangés.

## Risques
Changement de schéma : les lecteurs du CSV doivent tolérer 4 colonnes en plus (migration uniquement à la première
ligne opt-in). Le journal réel existant n'est pas modifié par cette PR.
