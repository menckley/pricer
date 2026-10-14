# pricer

Pricers de produits d'électricité France, construits uniquement sur des données gratuites et archivées :

- la **HPFC** horaire et les **settlements EEX** du dépôt [`power-hpfc`](https://github.com/menckley/power-hpfc) (lus sur son clone local) ;
- la courbe zéro-coupon de la **BCE** (API publique), pour l'actualisation.

Aucune donnée n'est inventée ni approchée : si une donnée manque (courbe non archivée, produit non coté, taux BCE non publié, heure hors de la courbe), le calcul s'arrête avec un message explicite.

## Contenu

| Élément | Rôle |
|---|---|
| `notebooks/pricer_structures_v1.ipynb` | Pricer de produits structurés linéaires (V1) |
| `pricings/<date de pricing>/<libellé>/` | Pricings archivés (voir section 7) |

## Installation

Le notebook utilise l'environnement où `power-hpfc` est installé en mode éditable :

```bash
cd ~/Documents/2-Coding/python/GitHub
../envir_lib/bin/python -m pip install -e "power-hpfc[dev,app]"
```

Les dépôts `power-hpfc` et `pricer` doivent être côte à côte dans le même dossier. La cellule 1.2 synchronise `power-hpfc` avec GitHub (`git pull --ff-only`), refuse de continuer si `data/` a été modifié localement, et vérifie que le paquet `hpfc` importé est bien celui de ce clone.

## Pricer V1 : produits linéaires

Un produit linéaire a un gain Σₕ qₕ · (Pₕ − K) : il se valorise avec la seule HPFC, sans volatilité.

| Section | Contenu | Contrôles intégrés |
|---|---|---|
| 1. Données | Synchronisation, chargement de la courbe et des settlements de la séance | La courbe reproduit chaque produit EEX coté (écart ≤ `arbitrage_tol`) |
| 2. Produits | `block` (Base / Peak / Offpeak), `shaped` (heures et jours sur mesure), `custom` (profil libre heure par heure), `eex` (produit coté) ; combinaisons `+`, `−`, `k ×` | Cal-27 Base = son settlement ; 64 produits cotés repricés ; Peak + Offpeak = Base |
| 3. Valorisation | Valeur face à K (constant ou horaire), non actualisée et actualisée ; prix juste future et prix juste OTC | Valeur nulle au prix juste ; linéarité ; changement de signe avec le sens |
| 4. Décomposition | Par mois, trimestre ou année, et par bloc Peak / Offpeak | Somme des lignes = total ; settlements mensuels retrouvés |
| 5. Couverture | Base + Peak sur le découpage le plus fin coté ; résidu et prime de forme | Produit EEX couvert sans résidu ; MWh de chaque bloc couverts exactement |
| 6. TBx | TB1, TB2, TB4 de la HPFC et du spot réalisé ; batterie en valeur intrinsèque ; backtest J+1 | Marge batterie = valorisation de son planning comme produit linéaire |
| 7. Archivage | Position, K, résultats et contexte de calcul | Relecture et reprice à l'identique |

## Conventions

- **Signe** : MW > 0 = achat d'électricité, MW < 0 = vente. Valeur = Σ q · (P − K), du point de vue de l'acheteur.
- **Grille** : heures UTC de la courbe, pas horaire vérifié ; heures locales Europe/Paris (jours de 23 h et 25 h compris).
- **Peak** : définition EEX, du lundi au vendredi de 8 h à 20 h, jours fériés inclus. Offpeak = toutes les autres heures.
- **Prix juste** : moyenne de la courbe pondérée par les volumes (prix d'un future EEX, sans actualisation car les marges sont appelées chaque jour). Non défini pour un produit qui mélange achat et vente.
- **Prix juste act.** : prix fixe d'un forward OTC payé mensuellement, pondéré par les facteurs d'actualisation.
- **Paiement** : le 20 du mois suivant la livraison (`PAY_DAY`, convention EFET).
- **Actualisation** : courbe BCE Svensson, obligations d'État AAA de la zone euro (`G_N_A`), du jour de pricing ; composition continue, décompte ACT/365. La formule est vérifiée contre les taux 1, 5 et 10 ans publiés.
- **Couverture** : par produit coté, MW Base = MWh offpeak ÷ heures offpeak, MW Peak = MWh peak ÷ heures peak − MW Base. Les positions affichées sont équivalentes au produit ; on couvre en prenant la position opposée.
- **TBx** : écart entre le prix moyen des x heures les plus chères et des x moins chères d'un jour, sans contrainte de chronologie.

## Ce que dit la HPFC sur les TBx

Le TBx est convexe en prix : le TBx d'une courbe de prix attendus est un plancher du TBx attendu.

Backtest sur 36 jours livrés (du 25/08 au 13/10/2026) : la base de la HPFC de la veille est égale à la base du spot réalisé (144,15 €/MWh de chaque côté). La HPFC capte 91 % du TB1 réalisé, 88 % du TB2 et 86 % du TB4. L'écart restant est la valeur extrinsèque, non valorisée en V1. L'échantillon couvre la fin de l'été et l'automne seulement ; il faut relancer la cellule 6.5 à mesure que l'archive s'allonge.

## Limites connues de la V1

- Les jours restants du mois en cours n'ont pas de produit mois / trimestre / an coté : ils sont signalés comme non couvrables.
- La batterie est valorisée en valeur intrinsèque seulement, à un cycle par jour, sans contrainte de chronologie ni d'usure : c'est un plancher, pas un prix de tolling.
- Le risque de forme est mesuré en valeur (prime de forme), pas en distribution : il faudrait un modèle de volatilité.

## Pistes d'amélioration

1. **Fourniture sur courbe de consommation client** : profils types Enedis (à vérifier : disponibilité et format) ou vraie courbe de charge du client, via `custom`.
2. **Couverture du mois en cours** avec les semaines, week-ends et jours EEX cotés.
3. **Batterie avec chronologie** : optimisation linéaire (état de charge, charge avant décharge, plusieurs cycles, usure).
4. **V2 : produits optionnels** : volatilité historique (à partir de l'archive spot et des settlements), Black-76, caps / floors / collars asiatiques, contrats à clics ; vérifier si EEX publie des options France.
5. **V3 : flexibilité de volume** : swing, take-or-pay.
6. **TBx en valeur extrinsèque** à partir d'un modèle de prix spot.
7. **PPA** : achat ou vente, pay-as-produced ou baseload, profils de production en trois niveaux (national ENTSO-E, site PVGIS / Open-Meteo, actif réel), GO, prix négatifs.
8. **Passage du notebook en paquet** `pricer` testé (pytest), sur le modèle de `power-hpfc`.
