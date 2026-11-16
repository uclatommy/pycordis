"""Typed service keys.

`Key[T]` names a service and carries the type of its value. Every pycordis function
that takes a key also takes the plain `str` name.
"""


class Key[T]:
    """The name of a service whose value has type `T`.

    A `Key` equals its name and has the hash of its name, so `Key("db")` and `"db"`
    select the same service.

    Examples:
        ```python
        >>> db = Key[dict]("db")
        >>> db == "db"
        True
        >>> {"db": 1}[db]
        1
        ```
    """

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        if not isinstance(name, str):
            raise TypeError(f"a key name must be a str, not {name!r}")
        self._name = name

    @property
    def name(self) -> str:
        """The service name."""
        return self._name

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Key):
            return self._name == other._name
        if isinstance(other, str):
            return self._name == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._name)

    def __repr__(self) -> str:
        return f"Key({self._name!r})"


type Keyable[T] = Key[T] | str
"""A service key, or its plain name."""
