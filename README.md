# pricer

Pricer de produits structurés électricité France, construit uniquement sur des données gratuites et archivées :

- la **HPFC** horaire, les **settlements EEX** et le **spot day-ahead** historique archivés par
  [`power-hpfc`](https://github.com/menckley/power-hpfc) (lus sur son clone local) ;
- l'**€STR** publié par la BCE (API publique), pour le coût de financement.

Aucune donnée n'est inventée ni approchée : une courbe absente, un produit non coté, un €STR non publié, une heure
hors de la courbe ou un paramètre non renseigné arrêtent le calcul avec un message explicite.

## Installation

Les dépôts `power-hpfc` et `pricer` sont côte à côte, `power-hpfc` déjà installé en mode éditable :

```bash
cd ~/Documents/2-Coding/python/GitHub
../envir_lib/bin/python -m pip install -e "pricer[notebook,dev]"
cd pricer && ../../envir_lib/bin/python -m pytest -q
```

Avant le premier pricing, renseigner les paramètres à `null` de `config/pricer.yaml` (frais EEX et day-ahead,
spread de financement) et de `config/marge.yaml` (politique de marge). La cellule « Données » du notebook liste
ceux qui manquent.

## Utilisation

`notebooks/pricer.ipynb` :

1. **Données** : synchronise power-hpfc avec GitHub (`git pull --ff-only`, refus si `data/` est modifié
   localement), charge la dernière courbe, vérifie qu'elle reproduit tous les settlements de la séance, charge le
   spot historique.
2. **Formulaire** : side (sell au client / buy à une contrepartie), settlement (physical delivery / financial
   swap), product, période, MW, hedging, margin, puis **Price** et **Save pricing**.

Produits disponibles :

| Produit | Description |
|---|---|
| Bloc | Base, Peak (définition EEX : lundi-vendredi 8h-20h, fériés inclus) ou Offpeak |
| Profil semaine | Grille 24 heures × 7 jours à cocher, appliquée à toutes les semaines de la période ; un jour férié suit la colonne de son jour de semaine |
| Produit EEX coté | Reconstruit à l'identique (mois, trimestre, année, Base ou Peak) |
| Profil heure par heure | CSV `horodatage` (avec fuseau), `mw` (positifs, le sens est choisi dans le formulaire) |
| Financial swap | N'importe lequel des profils ci-dessus, réglé contre le spot : buy = fixed-price payer, sell = fixed-price receiver |

## Construction du prix

| Poste | Calcul |
|---|---|
| Couverture EEX aux settlements | MW Base et Peak sur chaque produit coté du découpage, aux settlements de la séance |
| Résidu day-ahead attendu (HPFC) | Surplus revendus et manques achetés en day-ahead, valorisés sur la HPFC (prime de forme) |
| **= Fair price** | Moyenne de la HPFC pondérée par les volumes |
| Coût de risque du résidu | Quantile (95 %) du P&L du résidu, mesuré sur l'historique spot (voir ci-dessous) |
| Frais | `eex_eur_mwh` × MWh couverts + `epex_eur_mwh` × MWh traités en day-ahead |
| Prime de liquidité | `eur_mwh_par_mw` × (MW max traités sur un contrat − `seuil_mw`), sur les MWh couverts EEX |
| Conversion financier → physique | Livraison physique seulement : `conversion_fin_phys_eur_mwh` × tous les MWh du deal (les futures EEX France sont réglés financièrement) |
| Coût de financement | Ventes physiques seulement : sourcing payé à J+5, client à J+50 depuis le premier jour livré du mois, taux €STR + spread, ACT/360 |
| Marge | Règles de `config/marge.yaml`, ou saisie manuelle avec motif obligatoire |
| **= Offer price** | Fair price + postes (vente au client) ou − postes (achat à une contrepartie) |

### Couverture

Un produit identique à un contrat EEX coté (mêmes heures, MW constants : par exemple un Peak Cal-27 ou un bloc
Base Q2-27) se couvre par ce contrat lui-même : un seul contrat, prix juste = son settlement, rien en day-ahead,
coût de risque nul ; aucune stratégie n'est proposée. Les stratégies ne servent qu'aux produits profilés :

Le découpage le plus fin en produits cotés à la fois en Base et en Peak : les mois si les trois mois du
trimestre sont cotés, sinon le trimestre, sinon l'année. Sur chaque produit, MW Base = MWh offpeak ÷ heures
offpeak et MW Peak = MWh peak ÷ heures peak − MW Base : le résidu a un volume nul sur chaque bloc.
Trois stratégies de hedging sont évaluées à chaque pricing : `Base+Peak monthly` (mois cotés, sinon trimestre, sinon
année), `Base+Peak quarterly` (trimestre, sinon année) et `Base only monthly`. En `auto` (défaut), le pricer retient
la **moins chère** (risque + frais + conversion + liquidité + marge) ; à égalité, celle qui laisse le moins de MWh
en day-ahead, puis le moins de contrats EEX.
Les jours restants du mois en cours n'ont pas de produit mois / trimestre / an : ils restent dans le résidu.

### Coût de risque du résidu

Pour chaque mois livré, le résidu est résumé par sa semaine type (MW moyen par jour de semaine × heure). Cette
semaine type est rejouée sur chaque mois historique de la même saison (même mois calendaire ± `fenetre_mois`,
depuis `historique_debut`), recentrée sur un volume nul par bloc couvert, valorisée au spot réalisé, rapportée au
prix moyen du mois historique puis remise au niveau forward du mois livré. Coût = z(confiance) × σ du P&L total,
avec une corrélation uniforme `correlation_mois` entre mois (0 = indépendants). Les tableaux du pricing donnent
σ, moyenne, pire et meilleur cas et le nombre d'échantillons de chaque mois.

Hypothèses : P&L de loi normale pour le quantile ; corrélation uniforme entre mois. Non mesuré : le risque de
niveau d'un mois à l'intérieur d'un trimestre ou d'une année couverts en bloc, et le risque de niveau des volumes
sourcés en day-ahead sans couverture possible (mois en cours) : il faudrait des prix forward historiques, l'archive
des settlements ne remonte qu'au 24/08/2026. Le résultat affiche un avertissement dans ces deux cas.

## Archivage et journal des pricings

- **Journal sur GitHub (automatique, dans l'application Streamlit)** : chaque clic sur **Price** ajoute, en un seul
  commit dans ce dépôt, une ligne à `pricings/journal.csv` et un fichier léger
  `pricings/AAAA/MM/<horodatage>_<libellé>.json` (≈ 10 Ko) : définition compacte du produit (grille, bloc,
  contrat ; pas la série horaire), construction du prix, couverture, stratégies comparées, risque par mois, marge,
  avertissements et paramètres utilisés. Rien n'est jamais écrasé. Jeton : secret `PRICER_TOKEN` de l'application,
  *fine-grained*, limité à ce dépôt, **Contents : Read and write**. Sans jeton, le journal est désactivé.
- **Archive détaillée** : bouton « Download pricing (zip) » de l'application (position heure par heure,
  échantillons de risque, financement…) ; en local, aussi `exports/pricings/` (non versionné).

L'application écrit dans `pricings/` du dépôt : faire un `git pull` avant de pousser des modifications du code.

## Organisation du code

| Module | Rôle |
|---|---|
| `market.py` | Chargement de la séance (courbe, settlements, spot), contrôles, grille horaire |
| `products.py` | Produits et constructeurs (bloc, grille semaine, EEX, CSV, swap) |
| `valuation.py` | Prix juste, valeur face à K, décomposition par période et par bloc |
| `hedging.py` | Découpage coté, stratégies de couverture, résidu |
| `risk.py` | Coût de risque du résidu sur l'historique spot |
| `rates.py` | €STR BCE |
| `settings.py` | Paramètres et politique de marge |
| `quote.py` | Construction du prix |
| `tbx.py` | TBx, batterie en valeur intrinsèque, backtest HPFC / spot |
| `archive.py` | Archive détaillée (locale ou zip) |
| `journal.py` | Journal léger des pricings sur GitHub (un commit par pricing) |
| `ui.py` | Formulaire Jupyter (ipywidgets) |

## Pistes d'amélioration

1. **MW différents selon les heures** dans la grille semaine (aujourd'hui une case cochée = la même puissance).
2. **Grilles saisonnières ou mensuelles** (aujourd'hui une seule semaine type pour toute la période).
3. **Risque de niveau** : mois dans trimestre / année, et volumes du mois en cours sans couverture, quand l'archive
   des settlements sera assez longue pour comparer spot réalisé et forward.
4. **Couverture du mois en cours** avec les semaines, week-ends et jours EEX cotés.
5. **Pas de 15 minutes** : le day-ahead est au quart d'heure depuis le 01/10/2025, le risque est mesuré à l'heure.
6. **Fourniture sur courbe de consommation client** : profils types Enedis (à vérifier) ou courbe de charge réelle,
   avec le risque de volume ; jours fériés traités comme des dimanches pour ces profils.
7. **Batterie avec chronologie** : optimisation linéaire (état de charge, plusieurs cycles, usure).
8. **V2 : options** (call, put, cap, floor, collar, clics) : vérifier si l'API EEX publie des settlements d'options
   France, sinon volatilité historique sur un historique suffisant ; Black-76.
9. **V3 : flexibilité de volume** (swing, take-or-pay).
10. **PPA** : achat ou vente, pay-as-produced ou baseload, profils de production (national ENTSO-E, site
    PVGIS / Open-Meteo, actif réel), GO, prix négatifs.
11. **Plateforme Streamlit unique** (HPFC, settlements et GO, pricer, PPA) à la place de l'application HPFC
    actuelle : le formulaire devient une page, la grille semaine un tableau éditable à cases à cocher.
