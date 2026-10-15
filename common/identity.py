"""Name knowledge shared by the simulator and the Silver identity checks
(problem catalog M2): the common nicknames of each first name the simulator
uses. Lower case; a nickname maps back to one formal first name.

Kept small on purpose. A real deployment would use a maintained nickname
list; the point here is that the Silver check and the simulator's ground
truth read the same table, so the scoring measures the matching logic.
"""

NICKNAMES: dict[str, tuple[str, ...]] = {
    "james": ("jim", "jimmy", "jamie"),
    "mary": ("molly", "mae"),
    "robert": ("bob", "rob", "bobby"),
    "patricia": ("pat", "patty", "trish"),
    "linda": ("lindy",),
    "michael": ("mike", "mickey"),
    "barbara": ("barb", "babs"),
    "jennifer": ("jen", "jenny"),
    "david": ("dave", "davey"),
    "susan": ("sue", "suzy"),
}

# nickname -> formal first name
FORMAL_FIRST_NAME: dict[str, str] = {nick: formal for formal, nicks in NICKNAMES.items() for nick in nicks}


def formal_first_name(first_name: str) -> str:
    """The formal first name for a nickname; any other name unchanged (lower case)."""
    first = first_name.lower()
    return FORMAL_FIRST_NAME.get(first, first)
