"""
Tests for cosmonapse._url.synapse_from_url.

Covers the scheme dispatch and, specifically, that "memory://" is rejected:
MemorySynapse is in-process only and must be constructed directly rather
than reached through a URL (a URL would be ambiguous across processes).
"""

import pytest

from cosmonapse._url import synapse_from_url
from cosmonapse.synapse.dev import DevSynapse
from cosmonapse.synapse.kafka import KafkaSynapse
from cosmonapse.synapse.nats import NatsSynapse


def test_cosmo_scheme_builds_dev_synapse():
    synapse = synapse_from_url("cosmo://127.0.0.1:7070")
    assert isinstance(synapse, DevSynapse)


def test_nats_scheme_builds_nats_synapse():
    synapse = synapse_from_url("nats://127.0.0.1:4222")
    assert isinstance(synapse, NatsSynapse)


def test_kafka_scheme_builds_kafka_synapse():
    synapse = synapse_from_url("kafka://127.0.0.1:9092")
    assert isinstance(synapse, KafkaSynapse)


def test_memory_scheme_is_rejected():
    with pytest.raises(ValueError, match="memory"):
        synapse_from_url("memory://")


def test_unknown_scheme_is_rejected():
    with pytest.raises(ValueError, match="Unknown synapse URL scheme"):
        synapse_from_url("bogus://host:1234")
