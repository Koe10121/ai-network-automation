import os
import json
from kafka import KafkaConsumer
from dotenv import load_dotenv

load_dotenv()
bootstrap = os.getenv("KAFKA_BOOTSTRAP_SERVERS")
topic = os.getenv("KAFKA_TOPIC")
consumer = KafkaConsumer(
    topic,
    bootstrap_servers=bootstrap,
    group_id="config-worker",
    auto_offset_reset="earliest",
    value_deserializer=lambda m: json.loads(m.decode("utf-8")),
)
for message in consumer:
    print(message.value)