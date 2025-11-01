# processor/__init__.py
"""
Pacchetto `processor` – API pubblica per il server Flask.

Questo modulo espone le funzioni principali che prima vivevano tutte in un
unico gigantesco processor.py. Ora sono suddivise in più file specializzati:

- extract.py
    - phase_extract()
      Estrae toponimi dal PDF + genera PDF marcato, CSV, attestazioni JSON ecc.

- geocode.py
    - phase_geocode()
      Geocoding "legacy": per ogni riga/pagina.
    - phase_geocode_grouped()
      Geocoding "nuovo": una volta per toponimo, con progress callback,
      rispettando eventuali esclusioni utente.

- exclusions.py
    - load_user_exclusions(), save_user_exclusions(), apply_exclusions_to_csv()
      Gestione stato esclusioni (globali + per pagina) e rigenerazione CSV filtrato.

- utils.py
    - list_outputs(), group_toponyms()
      Utility per popolare il pannello download e i conteggi toponimici.

Il server Flask continuerà a fare:
    from processor import phase_extract, phase_geocode, ...
senza cambiare troppo.
"""

from .extract import phase_extract
from .geocode import phase_geocode, phase_geocode_grouped
from .utils import list_outputs, group_toponyms
from .exclusions import (
    load_user_exclusions,
    save_user_exclusions,
    apply_exclusions_to_csv,
)

__all__ = [
    "phase_extract",
    "phase_geocode",
    "phase_geocode_grouped",
    "list_outputs",
    "group_toponyms",
    "load_user_exclusions",
    "save_user_exclusions",
    "apply_exclusions_to_csv",
]
