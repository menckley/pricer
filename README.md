# pricer

Pricer de produits structurés électricité France, construit uniquement sur des données gratuites et archivées :

- la **HPFC** horaire, les **settlements EEX** et le **spot day-ahead** historique archivés par
  [`power-hpfc`](https://github.com/menckley/power-hpfc) (lus sur son clone local) ;
- l'**€STR** publié par la BCE (API publique), pour le coût de financement.

Aucune donnée n'est inventée : une courbe absente, un produit non coté, un €STR non publié, une heure hors de la
courbe, un historique insuffisant ou un paramètre non renseigné arrêtent le calcul avec un message explicite. La
seule approximation, la prime de déviation day-ahead, est mesurée sur des données réelles, prudente, et affichée
comme telle (voir plus bas).

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
   swap), product, période, MW, hedging (auto ou un scénario imposé, dont Full day-ahead), margin, puis **Price**
   et **Save pricing**.

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
| Prime de déviation day-ahead | Risque de niveau des MWh traités en day-ahead sans couverture EEX (tout le volume en full day-ahead, le mois en cours sinon) : proxy de persistance, voir ci-dessous |
| Frais | `eex_eur_mwh` × MWh couverts + `epex_eur_mwh` × MWh traités en day-ahead |
| Prime de liquidité | `eur_mwh_par_mw` × (MW max traités sur un contrat − `seuil_mw`), sur les MWh couverts EEX |
| Conversion financier → physique | Livraison physique seulement : `conversion_fin_phys_eur_mwh` × tous les MWh du deal, quel que soit le scénario (full day-ahead et contrats indexés compris) |
| Coût de financement | Ventes physiques seulement : sourcing payé à J+5, client à J+50 depuis le premier jour livré du mois, taux €STR + spread, ACT/360 |
| Marge | Règles de `config/marge.yaml`, ou saisie manuelle avec motif obligatoire. Assiette de la part « forme » : prime de forme du produit (valeur à la HPFC − mêmes MWh au prix moyen de leur bloc Peak / Offpeak du mois), identique dans tous les scénarios |
| **= Offer price** | Fair price + postes (vente au client) ou − postes (achat à une contrepartie) |

### Couverture

Un produit identique à un contrat EEX coté (mêmes heures, MW constants : par exemple un Peak Cal-27 ou un bloc
Base Q2-27) se couvre par ce contrat lui-même : un seul contrat, prix juste = son settlement, rien en day-ahead,
coût de risque nul ; aucune stratégie n'est proposée. Les stratégies ne servent qu'aux produits profilés :

Le découpage le plus fin en produits cotés à la fois en Base et en Peak : les mois si les trois mois du
trimestre sont cotés, sinon le trimestre, sinon l'année. Sur chaque produit, MW Base = MWh offpeak ÷ heures
offpeak et MW Peak = MWh peak ÷ heures peak − MW Base : le résidu a un volume nul sur chaque bloc.
Quatre scénarios sont évalués à chaque pricing : `Base+Peak monthly` (mois cotés, sinon trimestre, sinon année),
`Base+Peak quarterly` (trimestre, sinon année), `Base only monthly` et **`Full day-ahead`** (aucun contrat EEX :
tout le volume est acheté ou revendu en day-ahead ; frais day-ahead et conversion sur tout le volume, pas de prime
de liquidité ; risque = forme (même méthode que le résidu) + prime de déviation sur tout le volume). En `auto` (défaut),
le pricer retient le **moins cher** (risque + déviation + frais + conversion + liquidité + marge) ; à égalité, celui
qui laisse le moins de MWh en day-ahead, puis le moins de contrats EEX. Si un horizon du deal n'est pas mesurable
(historique trop court), le full day-ahead n'est pas évalué et le résultat le signale.
Les jours restants du mois en cours n'ont pas de produit mois / trimestre / an : ils restent dans le résidu, et leur
risque de niveau est la prime de déviation à l'horizon 0.

### Coût de risque du résidu

Pour chaque mois livré, le résidu est résumé par sa semaine type (MW moyen par jour de semaine × heure). Cette
semaine type est rejouée sur chaque mois historique de la même saison (même mois calendaire ± `fenetre_mois`,
depuis `historique_debut`), recentrée sur un volume nul par bloc couvert, valorisée au spot réalisé, rapportée au
prix moyen du mois historique puis remise au niveau forward du mois livré. Coût = z(confiance) × σ du P&L total,
avec une corrélation uniforme `correlation_mois` entre mois (0 = indépendants). Les tableaux du pricing donnent
σ, moyenne, pire et meilleur cas et le nombre d'échantillons de chaque mois.

Hypothèses : P&L de loi normale pour le quantile ; corrélation uniforme entre mois. Non mesuré : le risque de
niveau d'un mois à l'intérieur d'un trimestre ou d'une année couverts en bloc (il faudrait des prix forward
historiques ; l'archive des settlements ne remonte qu'au 24/08/2026) : le résultat affiche un avertissement.

### Prime de déviation day-ahead (proxy de persistance, prudent)

Risque que le day-ahead réalisé s'écarte du niveau de la HPFC pour les MWh achetés ou revendus en day-ahead sans
couverture. Faute d'historique forward gratuit (l'API EEX ne garde qu'environ 6 semaines de séances), il est mesuré
sur l'historique spot ENTSO-E avec une prévision **naïve**, plus grossière qu'une HPFC : la prime est donc
surestimée, ce qui est le sens prudent.

1. Origines : chaque jour historique à ± `fenetre_jour_du_mois` jours du jour du mois de la date de pricing.
2. Prévision par bloc (Peak, Offpeak) : moyenne spot des `fenetre_jours` derniers jours × saisonnalité du mois
   cible ÷ saisonnalité moyenne de la fenêtre. Saisonnalité d'un mois calendaire : moyenne, sur les années civiles
   complètes hors exclusion, de moyenne du mois ÷ moyenne de l'année.
3. Erreur relative = réalisé ÷ prévision − 1, sur le reste du mois (horizon 0) ou sur le mois k (horizon k mois).
4. Écart d'un horizon = RMSE de l'erreur (biais compris) ; au moins `min_mois_cibles` mois cibles distincts.
5. Deal : σ du mois = |valeur Peak| × RMSE Peak + |valeur Offpeak| × RMSE Offpeak (valeurs à la HPFC) ;
   σ = somme des mois (mois parfaitement corrélés) ; prime = z(confiance) × σ.

La crise 2021-09 → 2023-06 est exclue (ni origine, ni cible, ni saisonnalité) : `periodes_exclues: []` pour la
réintégrer. Tableaux : déviation par mois dans le résultat, RMSE par horizon dans l'archive détaillée.

> **À faire quand l'archive sera assez longue** : remplacer, horizon par horizon, ce proxy par l'erreur mesurée des
> HPFC archivées face au day-ahead réalisé, que `power-hpfc` calcule chaque jour (`python -m hpfc.cli accuracy`,
> fichiers `data/accuracy/market=FR/`). Critère proposé : pour un horizon donné, au moins 12 mois livrés distincts
> mesurés (soit environ 1 an d'archive pour l'horizon 1 mois, 2 ans pour 12 mois). En attendant, la prime reste
> étiquetée « persistence proxy (conservative) ».

## Contrats indexés spot : « EPEX spot ± spread »

Type de prix **EPEX spot indexed** (page Pricer, ou `pricer.indexation.quote_indexed`), livraison physique, sur
n'importe quel profil (bloc, grille, courbe horaire, contrat EEX).

- **Index du mois** = moyenne du prix day-ahead EPEX sur les heures livrées du mois, pondérée par les MWh de chaque
  heure, **arrondie au centime supérieur**. Prix facturé du mois = index + spread (vente au client) ou − spread
  (achat à un producteur) : un seul prix par mois.
- Chaque heure livrée est achetée (ou revendue) en day-ahead au prix de l'index : pas de couverture EEX, pas de
  risque de prix ni de forme. Le spread couvre des coûts :

| Composante du spread | Calcul |
|---|---|
| Frais day-ahead | `epex_eur_mwh` |
| Conversion financier → physique | `conversion_fin_phys_eur_mwh` |
| Financement | ventes : (€STR + spread) × (client_jours − sourcing_jours) / 360 sur la valeur de l'énergie à la HPFC (toute l'énergie est préfinancée) |
| Arrondi de l'index | achats seulement : l'arrondi au centime supérieur est payé au producteur, compté pour son maximum (0,01 €/MWh) ; pour une vente, le gain d'arrondi n'est pas déduit (prudence) |
| Marge | section `indexation` de `config/marge.yaml` (base, durée, plancher), ou manuelle avec motif |

- **Spread proposé** = spread exact arrondi au centime supérieur (ex. « EPEX spot + 0,69 €/MWh »). Le « prix tout
  compris attendu » (index attendu à la HPFC ± spread) est une estimation : le client paie l'index réalisé.
- Le coût de financement dépend du niveau du spot réalisé : il est estimé à la HPFC (avertissement affiché).
- Non couvert : écart de volume si le client ne consomme pas le profil contracté (fourniture sur courbe de charge,
  feuille de route B6-B7) ; swaps indexés (refusés : un swap « spot contre spot + spread » se réduit au spread).

## Options EEX : call, put, collar

Type de prix **Option on EEX Base future** (page Pricer, ou `pricer.option_quote.quote_option`) : options
européennes EEX sur les futures Base France mois, trimestre et année, à partir de l'archive quotidienne des
settlements d'options de power-hpfc (`data/eex/options/`).

| Élément | Calcul |
|---|---|
| Modèle | Black-76 sur le future, prime non actualisée (constaté sur les settlements EEX : parité exacte sans actualisation) ; échéance en ACT/365 |
| Échéance | règles EEX : option mois et trimestre, 4 jours de bourse avant la livraison, sauf janvier et Q1 : 3e jeudi de décembre ; option Cal : 2e jeudi de décembre ; calendrier EEX Derivatives (1er janvier, Vendredi saint, lundi de Pâques, 1er mai, 24, 25, 26, 31 décembre). Contrôle : un settlement archivé après l'échéance calculée arrête le pricing |
| Volatilité | **implicite des settlements EEX de la séance**, recalculée à chaque pricing : option hors de la monnaie inversée à chaque strike, interpolation linéaire entre strikes, prolongement à plat hors de la grille (signalé) |
| Contrôles | parité call − put, future implicite = settlement du future (au centime), calls décroissants et convexes, settlements inchangés depuis la séance précédente, volume et open interest |
| Comparaison | même structure à la **volatilité historique** du future (σ² = Σ r² / Σ Δt sur les settlements archivés, ± σ / √(2n)) ; non calculée sous `min_variations_historique` variations. Ce n'est pas une précision : ni l'une ni l'autre n'est « la vérité » |
| Prime proposée | jambes vendues à σ + `prime_vol_points`, achetées à σ − `prime_vol_points` ; + frais (`frais_eur_mwh` × MWh × jambes) + marge (section `options` de marge.yaml) pour une vente, − pour un achat |
| Greeks | delta, gamma, vega (par point de vol), theta (par jour), par MWh, position du client |

Structures : **call** (cap), **put** (floor), **collar** (le client achète le cap et vend le floor). Les settlements
d'options EEX n'avaient aucun volume ni open interest au 27/10/2026 : ce sont des valorisations EEX ; le pricer
l'affiche en avertissement.

Non couvert : options Peak (inexistantes sur EEX), options horaires ou journalières (caps sur le spot : simulation,
feuille de route C1), collar à prime nulle (recherche du strike), mesure de précision des volatilités face à la
volatilité réalisée jusqu'à l'échéance (à ajouter dans power-hpfc quand des options seront arrivées à échéance).

## Application en ligne

Le pricer est utilisé par l'application Streamlit du dépôt `power-hpfc` (page d'accueil **Pricer**, page
**Pricing history**). L'application télécharge ce dépôt avec le secret `PRICER_TOKEN` (jeton *fine-grained* limité à
`menckley/pricer`, **Contents : Read and write** : lecture du code, écriture du journal) et recharge le code moins de
5 minutes après un push qui modifie `src/` ou `config/`. Un 403 « Resource not accessible by personal access token »
à l'archivage = jeton en lecture seule.

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
| `deviation.py` | Prime de déviation day-ahead (proxy de persistance) |
| `indexation.py` | Contrats indexés spot EPEX : spread juste et index par mois |
| `options.py` | Calendrier EEX, échéances, Black-76, greeks, volatilité implicite, nappe et contrôles, volatilité historique |
| `option_quote.py` | Pricing des structures d'options (call, put, collar) au format du journal |
| `rates.py` | €STR BCE |
| `settings.py` | Paramètres et politique de marge |
| `quote.py` | Construction du prix |
| `tbx.py` | TBx, batterie en valeur intrinsèque, backtest HPFC / spot |
| `archive.py` | Archive détaillée (locale ou zip) |
| `journal.py` | Journal léger des pricings sur GitHub (un commit par pricing) |
| `ui.py` | Formulaire Jupyter (ipywidgets) |

## Limites connues (à garder en tête en lisant un prix)

1. **Prime de déviation = proxy de persistance** : prévision naïve, donc prime surestimée (prudent). À remplacer par
   l'erreur mesurée des HPFC archivées (voir « Prime de déviation » et la feuille de route, A1).
2. **Risque de niveau d'un mois dans un trimestre ou une année couverts en bloc non mesuré** (avertissement affiché) :
   pas d'historique forward gratuit, l'archive des settlements commence le 24/08/2026.
3. **Mois en cours** : aucun produit mois / trimestre / an ; les Day, Weekend et Week EEX cotés ne sont pas encore
   utilisés pour le couvrir (tout passe en day-ahead, avec la prime de déviation à l'horizon 0).
4. **Risque de forme en semaine type** : le résidu est résumé par une semaine type par mois ; P&L supposé normal
   pour le quantile ; corrélation entre mois = paramètre, pas une estimation.
5. **Pas horaire** alors que le day-ahead est au quart d'heure depuis le 01/10/2025.
6. **Financial swap** : des frais day-ahead sont comptés sur le résidu comme pour un deal physique, alors qu'un swap
   réglé contre le spot ne demande aucun ordre day-ahead. À trancher (garder par prudence ou retirer pour les swaps).
7. **Paramètres utilisateur** (frais, spread, liquidité, marge) : saisis à la main, non calibrés sur des données.
8. **Une seule semaine type** pour toute la période et **une case cochée = une même puissance**.
9. **Coûts de fourniture au client final non inclus** : capacité, CEE, acheminement (TURPE), taxes ; le prix est un
   prix « énergie de gros ».
10. **Pas de coût de marge (collatéral) EEX** : l'appel de marge initial et les variations sur la couverture ont un
    coût de financement non compté.

## Feuille de route

Ordre indicatif : A (fiabiliser ce qui existe) avant B et C (nouveaux produits), D et E en parallèle.

### A. Fiabiliser le pricing actuel

1. **Prime de déviation mesurée** : lire `data/accuracy/market=FR/daily.parquet` de power-hpfc, regrouper l'erreur
   de niveau (Peak, Offpeak) par horizon en mois, et basculer horizon par horizon dès 12 mois livrés distincts mesurés ;
   garder le proxy pour les horizons pas encore mesurables et afficher la source utilisée.
2. **Risque de niveau mois / trimestre / an** : quand l'archive des settlements couvrira au moins un an, mesurer
   l'écart historique entre un mois et son trimestre (ou son année) pour chiffrer la couverture trimestrielle.
3. **Couverture du mois en cours** par les Day, Weekend et Week EEX (déjà archivés par power-hpfc).
4. **Corrélation entre mois estimée** sur l'historique des P&L simulés au lieu d'un paramètre.
5. **Quart d'heure** : risque et position au pas de 15 minutes depuis le 01/10/2025.
6. **Coût du collatéral EEX** : marge initiale (paramètres de marge EEX / ECC publiés, à vérifier) × taux × durée,
   et coût des appels de marge de variation.
7. **Liquidité calibrée** : seuil et pente à partir des volumes et de l'open interest EEX par contrat (données
   publiques à vérifier) plutôt qu'en paramètres libres.
8. **Swaps** : trancher la question des frais day-ahead (limite 6).
9. **Marge** : grille par type de client et par produit ; marge minimum en €/MWh en plus du plancher en €.
10. **Pricing sur une date passée** (la courbe d'une séance archivée) pour rejouer un pricing ou comparer deux dates.

### B. Produits

**Produits conventionnels (à terme, fixes)**
1. Week, Weekend, Day EEX comme produits cotés (prix = settlement, couverture par eux-mêmes).
2. Saisonniers (été / hiver), blocs multi-heures non standards (ex. 7h-23h), profils par mois.
3. Grille semaine avec **MW différents par case**, et **grilles saisonnières / mensuelles**.
4. Produits indexés : **fait pour l'index spot EPEX** (v0.6.0). À ajouter : % fixe / % indexé, cap et floor sur
   l'index (collar : options), indexation gaz (PEG / TTF) et CO2 (linéaire sur les forwards, puis options sur
   écart de prix de type Kirk), moyenne de settlements EEX, inflation pour les PPA longs.
5. **Clics / tranches** : fixation progressive du prix d'un contrat annuel en plusieurs clics, chacun pricé à la
   date du clic ; historique des clics dans le journal.

**Fourniture sur courbe de consommation**
6. Profils types Enedis (à vérifier : profils publiés par catégorie) ou courbe de charge réelle du client (CSV) ;
   jours fériés traités comme des dimanches pour ces profils.
7. **Risque de volume** : écart entre courbe prévue et réalisée (température, activité), corrélé au prix ; coût
   de l'écart × prix de déséquilibre (RTE publie les prix de règlement des écarts).
8. Coûts de fourniture (limite 9) en lignes séparées, pour passer d'un prix de gros à un prix client.

**Options (V2)**
9. **Fait (v0.7.0) : call, put, collar** (voir « Options EEX »). Reste : collar à prime nulle, call / put spreads,
   EWMA et GARCH, précision des volatilités face au réalisé. Principe retenu, sur mois / trimestre / an : Black-76 sur le forward (le settlement du contrat),
   avec greeks (delta, gamma, vega, theta). Volatilité **recalculée à chaque pricing** sur toutes les séances
   archivées (fenêtre, nombre de points et intervalle de confiance enregistrés dans le journal), par tranche de délai
   avant livraison (effet Samuelson). Ordre de préférence : volatilité implicite des settlements d'options EEX
   France si l'API en publie (à vérifier en premier) ; sinon volatilité historique des settlements (refus sous
   60 séances par tranche, seuil en config ; erreur d'estimation ≈ σ / √(2n) : ± 9 % à 60 séances, ± 4,5 % à 250),
   avec EWMA en complément ; en attendant, volatilité d'un modèle à retour à la moyenne (Schwartz-Smith) calibré
   sur le spot ENTSO-E depuis 2019, étiquetée comme telle. GARCH(1,1) quand l'archive dépassera un an.
10. **Caps / floors horaires** (option sur chaque heure livrée) : simulation de prix horaires (C1) ; un cap horaire
    vaut bien plus qu'un cap sur la moyenne mensuelle.
11. **Combinaison de scénarios avec options** : comparer, pour un même deal, couverture par futures, par futures +
    options (ex. vente à prix fixe avec cap client, couverte par un call), ou day-ahead seul ; même règle « le moins
    cher gagne », avec le coût de la prime et du delta-hedging.
12. Options sur spread (spark / clean spark spread) : à terme, si des données gaz et CO2 gratuites sont intégrées.

**TBx et stockage**
13. TBx (TB1, TB2, TB4 : écart entre les N heures les plus chères et les N moins chères du jour) : valeur
    intrinsèque déjà dans `tbx.py` ; ajouter la **valeur extrinsèque** (simulation, C1), le backtest HPFC / spot sur
    l'archive et un produit « TBx » pricable dans l'application.
14. **Batterie avec chronologie** : optimisation linéaire (état de charge, rendement, puissance / énergie, cycles par
    jour, usure), sur la HPFC puis sur des scénarios simulés ; ajout éventuel des services système (FCR, aFRR :
    données RTE à vérifier).

**Flexibilité et structurés (V3)**
15. **Swing / take-or-pay** : volume minimum et maximum par jour, mois et an, pénalités ; valorisation par
    programmation dynamique ou Monte-Carlo à moindres carrés (Longstaff-Schwartz) sur des trajectoires simulées (C1).
16. **Tolling / virtual power plant** : option de production (centrale, coût marginal) = série d'options sur spread.
17. **PPA** : achat ou vente, pay-as-produced ou baseload, profil de production (national ENTSO-E, site PVGIS /
    Open-Meteo, actif réel), effet de cannibalisation solaire, prix négatifs (heures sans paiement), garanties
    d'origine (GO : données du dépôt `settlements-eex`), risque de profil et de volume.
18. Cadre post-ARENH (à vérifier : mécanisme en vigueur depuis le 01/01/2026) pour la fourniture de clients finaux.

### C. Modèle de prix

1. **Simulation de prix horaires** cohérente avec la HPFC : la HPFC comme moyenne, chocs de niveau mensuels
   (retour à la moyenne), chocs de forme tirés de l'historique (bootstrap des résidus), pics et prix négatifs ;
   calibrée sur le spot ENTSO-E (crise 2021-2023 exclue ou traitée à part). Indispensable aux options horaires, TBx
   extrinsèques, swing et batterie.
2. Validation de la simulation : la moyenne simulée doit reproduire la HPFC et les settlements, la dispersion doit
   reproduire l'erreur mesurée par power-hpfc.

### D. Gestion des risques et portefeuille

1. **Mark-to-market du journal** : réévaluer chaque pricing archivé (ou chaque deal conclu) sur la HPFC du jour,
   P&L et P&L explain (niveau, forme, temps).
2. **Statut des pricings** dans le journal : envoyé, gagné, perdu, conclu ; taux de transformation par produit.
3. Position agrégée du portefeuille par mois et par bloc, couverture à faire, VaR / quantiles sur l'historique.
4. **Risque de contrepartie** pour les deals longs (CVA simplifiée : probabilité de défaut × exposition future).

### E. Application et outillage

1. **Plateforme unique** : intégrer `settlements-eex` (settlements, GO) à l'application, puis les PPA.
2. **Fiche de proposition** (term sheet PDF) générée depuis un pricing.
3. Tests du pricer lancés par GitHub Actions à chaque push (comme power-hpfc).
4. Contrôle d'accès de l'application (Streamlit : application privée, viewers invités).
5. Notebook `pricer.ipynb` à garder aligné sur l'application (mêmes produits et scénarios).

## Reprise après une pause

- `git pull --rebase` dans `pricer` avant toute modification : l'application ajoute des commits dans `pricings/`.
- Vérifier que les paramètres de `config/pricer.yaml` et `config/marge.yaml` sont renseignés (la page Pricer liste
  ceux qui manquent), dont la section `deviation`.
- Vérifier que le jeton `PRICER_TOKEN` n'a pas expiré (GitHub → Settings → Developer settings → Fine-grained tokens).
- Regarder la page **HPFC accuracy** : nombre de mois livrés par horizon, pour savoir quand faire A1.
