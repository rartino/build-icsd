"""The store's entry-family declaration, shared byte-for-byte by both build passes.

The declaration is stamped into the database on first open and byte-checked on reopen,
so pass 1 (import) and pass 2 (canonicalization) must construct exactly the same
``SqlStore(entry_records=...)`` even though only pass 2 writes prototypes, protostructures
and runs.

Only the OPTIMADE ``structures`` family is declared. The pass-2 records
(``atomistic_protostructure``, ``atomistic_prototype``, ``core_run`` and
``cod_canonicalization``) are stored as on-demand internal tables, exactly like the
pass-1 ``cod_structure_import`` table: they need no entry declaration to be saved or
queried. Keeping them out of the entry declaration is deliberate -- adding them would make
the OPTIMADE server try to serve families that have no served definition yet (serving is
out of scope for now). A minimal declaration is the right default regardless. See README
"Two passes and the database declaration".
"""

from httk.atomistic import (
    ASUStructureRecord,
    FundamentalDomainStructureRecord,
    UnitcellStructureRecord,
)
from httk.atomistic.entries.structures import StructureEntry


def entry_records() -> dict[type, type | tuple[type, ...]]:
    """Return the entry-family declaration both passes open the store with."""
    return {StructureEntry: (UnitcellStructureRecord, FundamentalDomainStructureRecord, ASUStructureRecord)}
