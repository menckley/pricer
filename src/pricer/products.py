"""Produits linéaires : puissance signée heure par heure sur la grille de la courbe.

Convention : MW > 0 = on achète l'électricité (position longue), MW < 0 = on la vend.
Les constructeurs prennent une puissance positive et un sens « buy » / « sell ».
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from pricer.market import Market

SIDES = {"buy": 1.0, "sell": -1.0}
PROFILES = ("Base", "Peak", "Offpeak")
WEEKDAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")


@dataclass(frozen=True, eq=False)
class Product:
    """`physical` : True pour une livraison physique, False pour un swap financier (règlement contre le spot)."""
    name: str
    mw: pd.Series
    physical: bool = True

    def __post_init__(self):
        q = self.mw
        if q.empty:
            raise ValueError(f"{self.name} : aucune heure de livraison")
        if not isinstance(q.index, pd.DatetimeIndex) or str(q.index.tz) != "UTC":
            raise ValueError(f"{self.name} : index horodaté en UTC attendu")
        if not q.index.is_unique:
            raise ValueError(f"{self.name} : heures en double")
        if not np.isfinite(q.to_numpy(dtype=float)).all():
            raise ValueError(f"{self.name} : puissance manquante ou infinie")
        object.__setattr__(self, "mw", q.astype(float).sort_index())

    @property
    def volume(self) -> float:
        """MWh signés (pas horaire : 1 MW pendant 1 h = 1 MWh)."""
        return float(self.mw.sum())

    @property
    def abs_volume(self) -> float:
        return float(self.mw.abs().sum())

    @property
    def mixed(self) -> bool:
        return bool((self.mw > 0).any() and (self.mw < 0).any())

    @property
    def side(self) -> str:
        if self.mixed:
            return "mixed"
        return "buy" if self.volume > 0 else "sell"

    def _combine(self, other: "Product", sign: float, sep: str) -> "Product":
        if self.physical != other.physical:
            raise ValueError("on ne combine pas une livraison physique et un swap financier")
        q = self.mw.add(sign * other.mw, fill_value=0.0)
        q = q[q != 0]
        return Product(f"{self.name} {sep} {other.name}", q, self.physical)

    def __add__(self, other: "Product") -> "Product":
        return self._combine(other, 1.0, "+")

    def __sub__(self, other: "Product") -> "Product":
        return self._combine(other, -1.0, "−")

    def __neg__(self) -> "Product":
        return Product(f"-({self.name})", -self.mw, self.physical)

    def __rmul__(self, k: float) -> "Product":
        return Product(f"{k:g} × {self.name}", k * self.mw, self.physical)


def require_on_grid(mkt: Market, p: Product) -> None:
    if not p.mw.index.isin(mkt.curve.index).all():
        raise ValueError(f"{p.name} : heures hors de la courbe ({mkt.first_day} → {mkt.last_day})")


def _check_side(side: str) -> str:
    _sign(side)
    return side


def _sign(side: str) -> float:
    if side not in SIDES:
        raise ValueError(f"sens attendu : {', '.join(SIDES)}")
    return SIDES[side]


def _mw(mw: float) -> float:
    mw = float(mw)
    if not np.isfinite(mw) or mw <= 0:
        raise ValueError("puissance strictement positive attendue (le sens donne le signe)")
    return mw


VERB = {"buy": "Buy", "sell": "Sell"}


def _label(side: str, mw: float, what: str, start: date, end: date) -> str:
    return f"{VERB[side]} {mw:g} MW {what} {start:%d/%m/%Y}-{end:%d/%m/%Y}"


def block(mkt: Market, start: date, end: date, mw: float, side: str = "buy", profile: str = "Base",
          name: str | None = None) -> Product:
    """Bloc standard : Base, Peak (définition EEX) ou Offpeak (toutes les autres heures)."""
    s, mw = _sign(side), _mw(mw)
    m = mkt.days(start, end)
    if profile == "Peak":
        m &= mkt.is_peak
    elif profile == "Offpeak":
        m &= ~mkt.is_peak
    elif profile != "Base":
        raise ValueError(f"profil inconnu {profile!r} : {', '.join(PROFILES)}")
    return Product(name or _label(side, mw, profile, start, end), pd.Series(s * mw, index=mkt.curve.index[m]))


def grid_from_hours(hours, weekdays=range(7)) -> pd.DataFrame:
    """Grille semaine (24 heures × 7 jours) cochée sur des heures et des jours donnés."""
    g = empty_grid()
    for d in weekdays:
        for h in hours:
            g.iloc[h, d] = True
    return g


def empty_grid() -> pd.DataFrame:
    return pd.DataFrame(False, index=pd.RangeIndex(24, name="heure"), columns=list(WEEKDAYS))


def check_grid(grid) -> np.ndarray:
    g = np.asarray(grid, dtype=object)
    if g.shape != (24, 7):
        raise ValueError("grille attendue : 24 heures (lignes) × 7 jours (colonnes, lundi → dimanche)")
    if not all(isinstance(x, (bool, np.bool_)) for x in g.ravel()):
        raise ValueError("grille : cases cochées (True) ou non (False) uniquement")
    g = g.astype(bool)
    if not g.any():
        raise ValueError("grille : aucune heure cochée")
    return g


def weekly(mkt: Market, start: date, end: date, mw: float, grid, side: str = "buy",
           name: str | None = None) -> Product:
    """Profil de livraison défini par une semaine type cochée, appliqué à toute la période.
    Un jour férié suit la colonne de son jour de semaine. Heure locale : la case « 2 » couvre 2h-3h ;
    les jours de 25 h livrent deux fois la case 2, les jours de 23 h ne la livrent pas."""
    s, mw = _sign(side), _mw(mw)
    g = check_grid(grid)
    m = mkt.days(start, end).to_numpy()
    on = g[mkt.hour.to_numpy(), mkt.weekday.to_numpy()] & m
    if not on.any():
        raise ValueError("aucune heure cochée dans la période choisie")
    n_cells = int(g.sum())
    return Product(name or _label(side, mw, f"weekly profile ({n_cells} h/week)", start, end),
                   pd.Series(s * mw, index=mkt.curve.index[on]))


def shaped(mkt: Market, start: date, end: date, mw: float, hours, weekdays=range(7), side: str = "buy",
           name: str | None = None) -> Product:
    """Bloc sur mesure par heures locales et jours de semaine (0 = lundi) : raccourci de `weekly`."""
    hours, weekdays = list(hours), list(weekdays)
    if not set(hours) <= set(range(24)) or not set(weekdays) <= set(range(7)):
        raise ValueError("heures attendues dans 0…23, jours dans 0…6")
    return weekly(mkt, start, end, mw, grid_from_hours(hours, weekdays), side,
                  name or _label(side, mw, f"{min(hours)}h-{max(hours) + 1}h", start, end))


def custom(mkt: Market, mw: pd.Series, name: str) -> Product:
    """Profil libre : puissance signée par heure, index horodaté avec fuseau (UTC ou local)."""
    if not isinstance(mw.index, pd.DatetimeIndex) or mw.index.tz is None:
        raise ValueError(f"{name} : l'index doit être horodaté avec un fuseau horaire")
    p = Product(name, mw.tz_convert("UTC"))
    require_on_grid(mkt, p)
    return p


def read_profile_csv(path) -> pd.Series:
    """CSV à deux colonnes : horodatage (avec fuseau, ex. 2027-01-01T00:00:00+01:00) et mw (signé)."""
    df = pd.read_csv(path)
    if list(df.columns) != ["horodatage", "mw"]:
        raise ValueError("colonnes attendues : horodatage, mw")
    idx = pd.DatetimeIndex(pd.to_datetime(df["horodatage"], utc=True))
    return pd.Series(df["mw"].astype(float).to_numpy(), index=idx)


def quoted(mkt: Market, profile: str) -> list[str]:
    return list(mkt.settle.loc[mkt.settle["profile"] == profile, "name"])


def eex(mkt: Market, contract: str, profile: str = "Base", mw: float = 1.0, side: str = "buy") -> Product:
    """Produit EEX coté à la séance (ex. 'Cal-27', 'Q1-27', 'M11-26'), reconstruit à l'identique."""
    row = mkt.settle[(mkt.settle["name"] == contract) & (mkt.settle["profile"] == profile)]
    if len(row) != 1:
        raise KeyError(f"{profile} {contract} non coté le {mkt.pricing_date} ; cotés : {quoted(mkt, profile)}")
    r = row.iloc[0]
    mw = _mw(mw)
    return block(mkt, r["start"], r["end"], mw, side, profile, f"{VERB[_check_side(side)]} {mw:g} MW {profile} {contract}")


def as_swap(p: Product) -> Product:
    """Swap financier prix fixe contre spot sur le même profil : achat = payeur du fixe, vente = receveur du fixe."""
    if p.mixed:
        raise ValueError("swap : un seul sens attendu")
    role = "fixed-price payer" if p.side == "buy" else "fixed-price receiver"
    return Product(f"Financial swap, {role} — {p.name}", p.mw, physical=False)
