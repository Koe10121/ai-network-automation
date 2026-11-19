import json
import os

from dotenv import load_dotenv
from kafka import KafkaProducer

load_dotenv()

BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
TOPIC = os.getenv("KAFKA_TOPIC", "network-config-jobs")

_producer = None


def get_producer():
    global _producer
    if _producer is None:
        _producer = KafkaProducer(
            bootstrap_servers=BOOTSTRAP,
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
            key_serializer=lambda k: k.encode("utf-8"),
            acks="all",
            retries=3,
        )
    return _producer


def send_jobs(messages):
    """Send one message per device to the jobs topic.

    Each message is keyed by device_id, so jobs for the same device stay in order.
    Returns the Kafka partition and offset of every message.
    Raises an exception if Kafka cannot be reached or does not confirm delivery.
    """
    global _producer
    try:
        producer = get_producer()
        futures = [producer.send(TOPIC, key=m["device_id"], value=m) for m in messages]
        producer.flush(timeout=10)
        results = []
        for future in futures:
            metadata = future.get(timeout=10)
            results.append({"topic": metadata.topic, "partition": metadata.partition, "offset": metadata.offset})
        return results
    except Exception:
        _producer = None
        raise
