"""The store's entry-family declaration, shared byte-for-byte by both databases.

The declaration is stamped into each database on first open and byte-checked on reopen,
so pass 1 (import) and pass 2 (canonicalization) construct exactly the same
``SqlStore(entry_records=...)`` even though only pass 2 writes bare prototypes, bare protostructures
and runs.

Only the OPTIMADE ``structures`` family is declared. The pass-2 records
(``atomistic_bare_protostructure``, ``atomistic_bare_prototype``, ``core_run`` and
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
from httk.store import EntryIdScheme


def entry_records() -> dict[type, type | tuple[type, ...]]:
    """Return the structures-only entry declaration; pass-2 catalogs are on-demand tables."""
    return {StructureEntry: (UnitcellStructureRecord, FundamentalDomainStructureRecord, ASUStructureRecord)}


def entry_id_scheme() -> EntryIdScheme:
    """Return the stable COD entry-id namespace used by both build passes."""
    return EntryIdScheme("cod", "1")
