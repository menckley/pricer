"""Formulaire du pricer pour Jupyter (ipywidgets) : description du deal, grille semaine à cocher, résultats."""
from __future__ import annotations

from datetime import date

import ipywidgets as w
import pandas as pd
from IPython.display import display

from pricer import archive
from pricer.hedging import FULL_DA, STRATEGIES
from pricer.market import Market
from pricer.products import WEEKDAYS, as_swap, block, custom, eex, empty_grid, quoted, read_profile_csv, weekly
from pricer.quote import Quote, quote
from pricer.settings import Settings

PRODUCTS = [("Block (Base / Peak / Offpeak)", "block"), ("Weekly profile (grid)", "grid"),
            ("Listed EEX contract", "eex"), ("Hourly profile (CSV)", "csv")]
DAYS_EN = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class WeekGrid:
    """24 heures × 7 jours de cases à cocher, avec raccourcis."""

    def __init__(self):
        cell = w.Layout(width="44px", height="20px", padding="0px")
        self.cells = [[w.ToggleButton(value=False, description="", layout=cell) for _ in WEEKDAYS] for _ in range(24)]
        head = [w.Label("")] + [w.Label(d) for d in DAYS_EN]
        rows = []
        for h in range(24):
            rows += [w.Label(f"{h:02d}h-{h + 1:02d}h")] + self.cells[h]
        self.grid = w.GridBox(head + rows, layout=w.Layout(grid_template_columns="70px repeat(7, 46px)",
                                                             grid_gap="1px"))
        buttons = []
        for label, fn in (("All", lambda h, d: True), ("Clear", lambda h, d: False),
                          ("Weekdays 0h-24h", lambda h, d: d < 5), ("Peak Mon-Fri 8h-20h", lambda h, d: d < 5 and 8 <= h < 20),
                          ("Invert", None)):
            b = w.Button(description=label, layout=w.Layout(width="auto"))
            b.on_click(self._setter(fn))
            buttons.append(b)
        self.count = w.Label()
        self._bulk = False
        for row in self.cells:
            for c in row:
                c.observe(lambda _: self._update(), names="value")
        self._update()
        self.widget = w.VBox([w.HBox(buttons), self.grid, self.count])

    def _setter(self, fn):
        def apply(_):
            self._bulk = True
            for h in range(24):
                for d in range(7):
                    c = self.cells[h][d]
                    c.value = (not c.value) if fn is None else fn(h, d)
            self._bulk = False
            self._update()
        return apply

    def _update(self):
        if self._bulk:
            return
        n = int(self.values().to_numpy().sum())
        self.count.value = f"{n} hours ticked per week (holidays follow their weekday)"
        for row in self.cells:
            for c in row:
                c.button_style = "success" if c.value else ""

    def values(self) -> pd.DataFrame:
        g = empty_grid()
        for h in range(24):
            for d in range(7):
                g.iloc[h, d] = bool(self.cells[h][d].value)
        return g


class PricerForm:
    def __init__(self, mkt: Market, settings: Settings, spot: pd.Series | None = None):
        self.mkt, self.s, self.spot, self.last = mkt, settings, spot, None
        default_start = max(mkt.first_day, date(mkt.first_day.year + 1, 1, 1))
        style = {"description_width": "150px"}
        lay = w.Layout(width="400px")
        self.side = w.Dropdown(options=[("Sell (to client)", "sell"), ("Buy (from counterparty)", "buy")],
                               value="sell", description="Side", style=style, layout=lay)
        self.settle = w.Dropdown(options=[("Physical delivery", "physical"), ("Financial swap", "swap")],
                                 value="physical", description="Settlement", style=style, layout=lay)
        self.kind = w.Dropdown(options=PRODUCTS, value="grid", description="Product", style=style, layout=lay)
        self.start = w.DatePicker(value=default_start, description="Delivery start", style=style, layout=lay)
        self.end = w.DatePicker(value=date(default_start.year, 12, 31), description="Delivery end (incl.)",
                                style=style, layout=lay)
        self.mw = w.FloatText(value=1.0, description="Power MW", style=style, layout=lay)
        self.profile = w.Dropdown(options=["Base", "Peak", "Offpeak"], value="Base", description="Block",
                                  style=style, layout=lay)
        self.eex_profile = w.Dropdown(options=["Base", "Peak"], value="Base", description="EEX profile",
                                      style=style, layout=lay)
        self.contract = w.Dropdown(options=quoted(mkt, "Base"), description="Contract", style=style, layout=lay)
        self.eex_profile.observe(lambda ch: setattr(self.contract, "options", quoted(mkt, ch["new"])), names="value")
        self.path = w.Text(value="", placeholder="CSV path (columns horodatage, mw)", description="File",
                           style=style, layout=w.Layout(width="600px"))
        self.week = WeekGrid()
        self.strategy = w.Dropdown(options=[("Auto (cheapest scenario)", "auto")] + [(k, k) for k in (*STRATEGIES, FULL_DA)],
                                   value=settings.strategy, description="Hedging", style=style, layout=lay)
        self.margin_mode = w.Dropdown(options=[("Rules (config/marge.yaml)", "rules"), ("Manual", "manual")],
                                      value="rules", description="Margin", style=style, layout=lay)
        self.margin_value = w.FloatText(value=0.0, description="Margin €/MWh", style=style, layout=lay)
        self.margin_reason = w.Text(value="", description="Reason", style=style, layout=w.Layout(width="600px"))
        self.run = w.Button(description="Price", button_style="primary")
        self.label = w.Text(value="", description="Label", style=style, layout=w.Layout(width="600px"))
        self.save = w.Button(description="Save pricing")
        self.out, self.save_out = w.Output(), w.Output()
        self.specific = w.VBox()
        self.margin_box = w.VBox()
        self.hedge_box = w.VBox()
        self.hedge_note = w.HTML("<i>Hedging: the listed contract itself (no strategy to choose)</i>")
        self.kind.observe(lambda _: self._refresh(), names="value")
        self.margin_mode.observe(lambda _: self._refresh(), names="value")
        self.run.on_click(lambda _: self.price())
        self.save.on_click(lambda _: self.archive())
        self._refresh()
        self.widget = w.VBox([
            w.HTML(f"<b>{mkt.summary()}</b>"),
            w.HBox([w.VBox([self.side, self.settle, self.kind, self.start, self.end, self.mw]),
                    w.VBox([self.hedge_box, self.margin_mode, self.margin_box])]),
            self.specific, self.run, self.out, w.HBox([self.label, self.save]), self.save_out])

    def _refresh(self):
        k = self.kind.value
        dated = k in ("block", "grid")
        self.start.disabled = self.end.disabled = not dated
        self.mw.disabled = k == "csv"
        self.specific.children = {"block": [self.profile], "grid": [self.week.widget],
                                  "eex": [self.eex_profile, self.contract], "csv": [self.path]}[k]
        self.margin_box.children = [self.margin_value, self.margin_reason] if self.margin_mode.value == "manual" else []
        self.hedge_box.children = [self.hedge_note] if k == "eex" else [self.strategy]

    def product(self):
        k, side, m = self.kind.value, self.side.value, self.mkt
        if k in ("block", "grid") and (self.start.value is None or self.end.value is None):
            raise ValueError("dates de début et de fin à renseigner")
        if k == "block":
            p = block(m, self.start.value, self.end.value, self.mw.value, side, self.profile.value)
        elif k == "grid":
            p = weekly(m, self.start.value, self.end.value, self.mw.value, self.week.values(), side)
        elif k == "eex":
            if not self.contract.value:
                raise ValueError("aucun contrat sélectionné")
            p = eex(m, self.contract.value, self.eex_profile.value, self.mw.value, side)
        else:
            mw = read_profile_csv(self.path.value).abs()
            p = custom(m, -mw if side == "sell" else mw,
                       f"{'Sell' if side == 'sell' else 'Buy'} hourly profile {self.path.value.split('/')[-1]}")
        return as_swap(p) if self.settle.value == "swap" else p

    def price(self) -> Quote | None:
        self.out.clear_output()
        with self.out:
            try:
                p = self.product()
                manual = self.margin_mode.value == "manual"
                q = quote(self.mkt, p, self.s, self.strategy.value, self.spot,
                          self.margin_value.value if manual else None, self.margin_reason.value)
            except (ValueError, KeyError, LookupError, FileNotFoundError) as e:
                print(f"✗ {e}")
                self.last = None
                return None
            self.last = q
            show(q)
            if not self.label.value:
                self.label.value = p.name[:60]
            return q

    def archive(self):
        self.save_out.clear_output()
        with self.save_out:
            if self.last is None:
                print("✗ nothing to save: click « Price » first")
                return
            try:
                path = archive.save_quote(self.mkt, self.last, self.label.value)
            except (ValueError, FileExistsError) as e:
                print(f"✗ {e}")
                return
            print(f"Saved in {path}")


def show(q: Quote) -> None:
    kind = "physical" if q.product.physical else "financial"
    print(f"{q.product.name}\n{q.side} — {kind} — {q.volume:,.0f} MWh — offer price {q.price:,.2f} €/MWh "
          f"(fair price {q.fair_price:,.2f}) — hedging: {q.context['strategy selected']}")
    for msg in q.warnings:
        print(f"⚠ {msg}")
    display(q.lines.round(2))
    if len(q.strategies):
        print("Scenarios compared (auto = cheapest; tie: least day-ahead volume, then fewest contracts)")
        display(q.strategies.round(2))
    print("EEX hedge (position equivalent to the deal: hedge = trade the opposite side)")
    display(q.hedge.table.round(2))
    display(pd.Series(q.hedge_values, name="hedge and residual").to_frame().round(2))
    print(f"Residual risk cost: {q.risk.cost:,.0f} € (σ {q.risk.sigma:,.0f} €, confidence {q.risk.confidence:.0%}, "
          f"month correlation {q.risk.correlation:g})")
    if len(q.risk.by_month):
        display(q.risk.by_month.round(0))
    if q.deviation is not None and len(q.deviation.by_month):
        print(f"Day-ahead deviation premium: {q.deviation.cost:,.0f} € (σ {q.deviation.sigma:,.0f} €, "
              f"{q.deviation.method})")
        display(q.deviation.by_month.round(2))
    if len(q.financing):
        print("Funding of sourcing")
        display(q.financing.round(2))
    print("Margin:", {k: (round(v, 2) if isinstance(v, float) else v) for k, v in
                      {"€": q.margin.eur, "rule": q.margin.rule, **q.margin.detail}.items()})
