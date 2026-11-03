"""Cooperative cancellation shared by local queries and downloads."""


class OperationCancelled(Exception):
    pass


def check_cancelled(cancelled):
    if cancelled():
        raise OperationCancelled("Operação cancelada.")
