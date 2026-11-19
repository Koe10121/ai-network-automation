import os
import json
import uuid
from kafka import KafkaProducer
from kafka.errors import KafkaError
from dotenv import load_dotenv
load_dotenv()
bootstrap = os.getenv("KAFKA_BOOTSTRAP_SERVERS")
topic = os.getenv("KAFKA_TOPIC")
producer = KafkaProducer(
    bootstrap_servers=bootstrap,
    value_serializer=lambda v: json.dumps(v).encode("utf-8"),
)
job = {
    "job_id": str(uuid.uuid4()),
    "device_id": "R1",
    "config_ref": "test-config-001",
}
try:
    future = producer.send(topic, value=job)
    metadata = future.get(timeout=10)
    print(metadata.topic, metadata.partition, metadata.offset)
except KafkaError as e:
    print(e)
    producer.flush()
producer.close()