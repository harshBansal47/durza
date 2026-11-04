"""Helpers for user-typed search terms.

`ILIKE '%term%'` treats `%` and `_` in the term as wildcards. Typing `%` therefore
matched every row (the most expensive possible query, on the one endpoint anyone can
call), and `50%` or `a_b` matched things nobody asked for. Search terms are literals, so
the wildcard characters are escaped and the statement says which escape character it used.
"""

LIKE_ESCAPE = "\\"


def like_contains(term: str) -> str:
    """A `%term%` pattern that matches `term` literally. Pair with `escape=LIKE_ESCAPE`."""
    escaped = (
        term.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", LIKE_ESCAPE + "%")
        .replace("_", LIKE_ESCAPE + "_")
    )
    return f"%{escaped}%"
