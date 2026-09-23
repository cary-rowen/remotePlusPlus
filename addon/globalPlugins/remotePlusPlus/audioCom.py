"""Keep device destruction inside the COM apartment, including exception paths."""

from contextlib import contextmanager
from collections.abc import Iterator
import traceback


def clearExceptionFrames(error: BaseException) -> None:
	# Completed frames can still hold COM pointers through a method's self.
	# Preserve traceback locations for logging, but release frame locals now.
	pending = [error]
	seen: set[int] = set()
	while pending:
		current = pending.pop()
		if id(current) in seen:
			continue
		seen.add(id(current))
		traceback.clear_frames(current.__traceback__)
		if current.__cause__ is not None:
			pending.append(current.__cause__)
		if current.__context__ is not None:
			pending.append(current.__context__)


@contextmanager
def comApartment() -> Iterator[None]:
	import comtypes

	comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
	try:
		yield
	except BaseException as error:
		clearExceptionFrames(error)
		raise
	finally:
		comtypes.CoUninitialize()
