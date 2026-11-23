"""Arm `memoryking_semantic`: `memoryking` with the semantic channel on, as `memoryking setup` leaves a
store once the embeddings libraries and the model are present (see `memoryking.create_semantic`)."""

from .memoryking import create_semantic as create  # noqa: F401
