"""Ordered collections of revertible registrations.

`DisposableList` is the collection to use for registrations such as event listeners,
where each registration returns a function that removes it.
"""

from collections.abc import Callable, Iterator
from itertools import count


class DisposableList[T]:
    """An ordered collection of registrations, each removed by its own disposer.

    Use `push` to add a registration after the others and `unshift` to add it before
    them. Iteration gives the `unshift` entries newest first, then the `push` entries
    oldest first. Entries are compared by identity, so two equal values are two
    entries when they are two objects.

    Examples:
        ```python
        >>> entries = DisposableList[str]()
        >>> remove = entries.push("b")
        >>> _ = entries.unshift("a")
        >>> list(entries)
        ['a', 'b']
        >>> remove()
        >>> list(entries)
        ['a']
        ```
    """

    def __init__(self) -> None:
        self._serial = count(1)
        self._head: dict[int, T] = {}
        self._tail: dict[int, T] = {}
        # The list holds each entry strongly, so an entry's `id` cannot be reused
        # while the entry is registered. `_serials` maps it to its newest serial.
        self._serials: dict[int, int] = {}

    def __len__(self) -> int:
        return len(self._head) + len(self._tail)

    def __iter__(self) -> Iterator[T]:
        """Iterate over a snapshot, so a loop body can add or remove entries."""
        return iter([*reversed(self._head.values()), *self._tail.values()])

    def push(self, value: T) -> Callable[[], None]:
        """Add `value` after every other entry.

        Returns:
            A function that removes this entry. Calling it again does nothing.
        """
        serial = next(self._serial)
        self._tail[serial] = value
        self._serials[id(value)] = serial
        return lambda: self._remove(serial)

    def unshift(self, value: T) -> Callable[[], None]:
        """Add `value` before every other entry.

        Returns:
            A function that removes this entry. Calling it again does nothing.
        """
        serial = -next(self._serial)
        self._head[serial] = value
        self._serials[id(value)] = serial
        return lambda: self._remove(serial)

    def delete(self, value: T) -> bool:
        """Remove `value`, which is the object that was added.

        When the same object was added more than once, this removes the newest entry.

        Returns:
            `True` if an entry was removed, `False` if `value` is not in the list.
        """
        serial = self._serials.get(id(value))
        return serial is not None and self._remove(serial)

    def clear(self) -> list[T]:
        """Remove every entry.

        Returns:
            The removed entries, in iteration order.
        """
        values = list(self)
        self._head.clear()
        self._tail.clear()
        self._serials.clear()
        return values

    def _remove(self, serial: int) -> bool:
        table = self._head if serial < 0 else self._tail
        if serial not in table:
            return False
        value = table.pop(serial)
        if self._serials.get(id(value)) == serial:
            del self._serials[id(value)]
        return True
