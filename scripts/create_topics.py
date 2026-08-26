"""Create every Kafka topic the platform needs.

Topics are created explicitly rather than relying on auto-creation
(`KAFKA_AUTO_CREATE_TOPICS_ENABLE` is off in docker-compose). Auto-created
topics take broker defaults -- usually one partition -- which quietly caps
consumer parallelism forever, and a typo'd topic name silently becomes a real
topic that nothing reads.

Run after the broker is up:

    python scripts/create_topics.py
"""

from __future__ import annotations

import argparse
import sys

sys.path.insert(0, "libs/retailpulse_common")

from confluent_kafka.admin import AdminClient, NewTopic  # noqa: E402

from retailpulse_common.events.topics import ALL_DLQ_TOPICS, ALL_TOPICS  # noqa: E402

# Three partitions gives room for three consumers per group to work in
# parallel. Partition count can only be increased later, and increasing it
# changes key-to-partition mapping, so it is worth not starting at 1.
DEFAULT_PARTITIONS = 3

# One broker locally, so replication cannot exceed 1. Production would use 3
# with min.insync.replicas=2, which is what makes acks=all meaningful.
DEFAULT_REPLICATION = 1

# Dead-letter topics stay single-partition: strict arrival ordering is more
# useful than throughput when a human is reading them.
DLQ_PARTITIONS = 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Create RetailPulse Kafka topics.")
    parser.add_argument("--bootstrap-servers", default="localhost:9092")
    parser.add_argument("--partitions", type=int, default=DEFAULT_PARTITIONS)
    parser.add_argument("--replication-factor", type=int, default=DEFAULT_REPLICATION)
    args = parser.parse_args()

    admin = AdminClient({"bootstrap.servers": args.bootstrap_servers})

    existing = set(admin.list_topics(timeout=15).topics)

    wanted: list[NewTopic] = []
    for topic in ALL_TOPICS:
        if topic not in existing:
            wanted.append(
                NewTopic(
                    topic,
                    num_partitions=args.partitions,
                    replication_factor=args.replication_factor,
                    config={
                        # Seven days of retention: long enough to replay a
                        # weekend incident, short enough to bound disk.
                        "retention.ms": str(7 * 24 * 60 * 60 * 1000),
                    },
                )
            )
    for topic in ALL_DLQ_TOPICS:
        if topic not in existing:
            wanted.append(
                NewTopic(
                    topic,
                    num_partitions=DLQ_PARTITIONS,
                    replication_factor=args.replication_factor,
                    config={
                        # Dead letters are kept far longer -- they exist to be
                        # investigated, sometimes days after the fact.
                        "retention.ms": str(30 * 24 * 60 * 60 * 1000),
                    },
                )
            )

    if not wanted:
        print(f"All {len(ALL_TOPICS) + len(ALL_DLQ_TOPICS)} topics already exist.")
        return 0

    failures = 0
    for topic, future in admin.create_topics(wanted).items():
        try:
            future.result()
            print(f"  created  {topic}")
        except Exception as exc:  # noqa: BLE001
            if "already exists" in str(exc).lower():
                print(f"  exists   {topic}")
            else:
                print(f"  FAILED   {topic}: {exc}", file=sys.stderr)
                failures += 1

    print(
        f"\n{len(wanted) - failures} topic(s) created, {failures} failed. "
        f"{len(ALL_TOPICS)} business topics + {len(ALL_DLQ_TOPICS)} dead-letter topics total."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
