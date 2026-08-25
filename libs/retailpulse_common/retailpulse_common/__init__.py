"""Shared building blocks used by every RetailPulse service.

Nothing in here contains business logic. It exists so that cross-cutting
concerns -- configuration, database sessions, error shapes, health probes,
metrics, auth and the Kafka event envelope -- behave identically across all
nine services instead of being re-implemented (and subtly diverging) in each.
"""

__version__ = "0.1.0"
