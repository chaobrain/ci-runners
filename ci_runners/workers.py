"""Validation and refusal evidence for a plan's optional worker override."""
import argparse

REASON = 'workers: '


def positive(value: object) -> bool:
    return type(value) is int and value > 0


def cli_count(value: str, field: str = 'workers') -> int:
    if value.isascii() and value.isdecimal():
        try:
            n = int(value)
        except ValueError:
            pass
        else:
            if positive(n):
                return n
    raise argparse.ArgumentTypeError(f'{field} must be a positive integer')


def refusal(requested: int, reason: str) -> dict:
    return {'unavailable': REASON + reason, 'workers': {'requested': requested, 'command_started': False}}
