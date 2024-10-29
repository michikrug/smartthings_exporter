import aiohttp
import asyncio
import logging as log
import os
import signal
import sys
import time
import pysmartthings

from dotenv import load_dotenv
from prometheus_client import REGISTRY, Gauge, start_http_server

load_dotenv()


class SmartThingsMetricException(Exception):
    pass


class SmartThingsMetric:
    def __init__(self, payload_key: str, device_name: str, documentation: str = None):
        self.payload_key = payload_key
        self.device_name = device_name
        self.name = f"smartthings_{self.payload_key}"
        self.metric = Gauge(self.name, documentation or f"value from API object key {payload_key}", labelnames=["device"])
        self.value = None
        self.last_update_time = None

    def set(self, value):
        log.debug(f"Set {self.name} = {value}")

        if self.value != value:
            self.metric.labels(device=self.device_name).set(value)
            self.value = value
            self.last_update_time = time.time()

    def clear(self):
        log.debug(f"Clear {self.name}")
        self.metric.clear()
        self.last_update_time = time.time()


class Worker:
    def __init__(self, smartthings_token: str, device_id: str, device_name: str, device_metrics: list[str], collecting_interval_seconds: int = 30, expiration_threshold: int = 300):
        self.smartthings_token = smartthings_token
        self.device_id = device_id
        self.device_name = device_name
        self.device_metrics = device_metrics
        self.collecting_interval_seconds = collecting_interval_seconds
        self.metrics_collector: list[SmartThingsMetric] = []
        self.expiration_threshold = expiration_threshold
        self.running = True

    async def loop(self):
        async with aiohttp.ClientSession() as session:
            api = pysmartthings.SmartThings(session, self.smartthings_token)
            device = await api.device(self.device_id)
            while self.running:
                try:
                    await device.status.refresh()
                    for payload_key in self.device_metrics:
                        metric = self.get_metric_by_payload_key(payload_key)
                        if metric:
                            metric.set(device.status.values.get(payload_key))

                except Exception as error:
                    log.error(f"Error processing payload: {error}")
                self.clear_expired_metrics()
                await asyncio.sleep(self.collecting_interval_seconds)

    def stop(self):
        self.running = False

    def clear_expired_metrics(self):
        # Clear metrics that haven't been updated for more than the expiration threshold in seconds
        current_time = time.time()
        for metric in self.metrics_collector:
            if current_time - metric.last_update_time > self.expiration_threshold:
                metric.clear()
                log.info(f"Cleared expired metric {metric.name}")

    def create_new_metric(self, payload_key: str) -> SmartThingsMetric:
        try:
            metric = SmartThingsMetric(payload_key, self.device_name)
            log.info(f"Created new metric from payload key {metric.payload_key} -> {metric.name}")
            return metric
        except SmartThingsMetricException as error:
            log.error(error)
            return None

    def get_metric_by_payload_key(self, payload_key: str) -> SmartThingsMetric:
        # Find the metric linked to the provided SmartThings payload key, or create a new one if not found
        metric = next((metric for metric in self.metrics_collector if metric.payload_key == payload_key), None)
        if metric:
            log.debug(f"Found metric {metric.name} linked to {payload_key}")
        else:
            log.debug(f"Cannot find metric linked to {payload_key}. Creating new metric")
            metric = self.create_new_metric(payload_key)
            self.metrics_collector.append(metric)
        return metric


def signal_handler(signum: int, frame: object | None) -> None:
    log.info(f"Received signal {signum}. Exiting...")
    sys.exit(0)


def load_env_variable(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        log.error(f"Environment variable {name} is required.")
        sys.exit(1)
    return value


def main() -> None:
    # Register the signal handler for SIGTERM
    signal.signal(signal.SIGTERM, signal_handler)

    # Disable Process and Platform collectors
    for collector in list(REGISTRY._collector_to_names.keys()):
        REGISTRY.unregister(collector)

    log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(log, log_level_str, log.INFO)
    log.basicConfig(stream=sys.stdout, level=log_level, format="%(asctime)s %(levelname)-7s %(message)s")

    device_id = load_env_variable("DEVICE_ID")
    device_name = os.getenv("DEVICE_NAME", device_id)
    device_metrics = load_env_variable("DEVICE_METRICS").split(",")
    smartthings_token = load_env_variable("SMARTTHINGS_TOKEN")
    exporter_port = int(os.getenv("EXPORTER_PORT", "9090"))
    collecting_interval_seconds = int(os.getenv("COLLECTING_INTERVAL", "30"))
    expiration_threshold = int(os.getenv("EXPIRATION_THRESHOLD", "300"))

    metrics = Worker(smartthings_token, device_id, device_name, device_metrics, collecting_interval_seconds, expiration_threshold)

    start_http_server(exporter_port)

    try:
        asyncio.run(metrics.loop())
    except KeyboardInterrupt:
        log.info("Received KeyboardInterrupt. Exiting...")
        metrics.stop()
        sys.exit(0)


if __name__ == "__main__":
    main()
